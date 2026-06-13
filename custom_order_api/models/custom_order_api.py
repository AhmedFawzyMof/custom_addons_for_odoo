import json
import uuid
from odoo import models, fields, api, _
from odoo.exceptions import UserError

class CustomOrderApi(models.AbstractModel):
    _name = 'custom.order.api'
    _description = 'Headless Order API Handler'

    def _get_company_domain(self):
        cids = self.env.context.get('allowed_company_ids', [])
        if cids:
            return [('company_id', 'in', cids)]
        return []

    # -------------------------------------------------------------------------
    # 1. GET /api/orders -> api_get_orders
    # -------------------------------------------------------------------------
    @api.model
    def api_get_orders(self, params=None, **kwargs):
        """
        Fetch POS orders with dynamic filtering, search, and pagination.
        """
        if isinstance(params, list) and len(params) == 1 and isinstance(params[0], dict):
            params = params[0]
        if isinstance(params, dict):
            kwargs.update(params)

        page        = int(kwargs.get('page',        1))
        limit       = int(kwargs.get('limit',       10))
        search_term = kwargs.get('search_term')
        status      = kwargs.get('status')
        date_from   = kwargs.get('date_from')
        date_to     = kwargs.get('date_to')
        session_id  = kwargs.get('session_id')

        STATUS_MAP = {
            'draft':     'draft',
            'cancel':    'cancel',
            'cancelled': 'cancel',
            'paid':      'paid',
            'done':      'done',
            'invoiced':  'invoiced',
        }

        domain = self._get_company_domain()

        if status and status.lower() not in ('all', ''):
            odoo_state = STATUS_MAP.get(status.lower(), status)
            domain.append(('state', '=', odoo_state))

        if date_from:
            domain.append(('date_order', '>=', date_from))
        if date_to:
            domain.append(('date_order', '<=', date_to))

        if session_id and str(session_id).strip() not in ('', 'null', 'undefined'):
            domain.append(('session_id.name', 'ilike', str(session_id).strip()))

        if search_term:
            domain = ['|',
                      ('name', 'ilike', search_term),
                      ('partner_id.name', 'ilike', search_term)] + domain

        offset = (page - 1) * limit
        
        order_records = self.env['pos.order'].search(
            domain, offset=offset, limit=limit, order='date_order desc'
        )
        total_count = self.env['pos.order'].search_count(domain)

        orders_data = []
        for order in order_records:
            orders_data.append({
                'id': order.id,
                'name': order.pos_reference or order.name,
                'date_order': order.date_order.isoformat() if order.date_order else None,
                'state': order.state,
                'partner_id': [order.partner_id.id, order.partner_id.name] if order.partner_id else False,
                'user_id': [order.user_id.id, order.user_id.name] if order.user_id else False,
                'session_id': [order.session_id.id, order.session_id.name] if order.session_id else False,
                'amount_total': order.amount_total,
                'amount_paid': order.amount_paid,
                'amount_tax': order.amount_tax,
                'amount_return': order.amount_return,
            })

        return {
            'data': orders_data,
            'totalItems': total_count,
            'totalPages': (total_count + limit - 1) // limit,
            'currentPage': page,
            'itemsPerPage': limit,
        }

    # -------------------------------------------------------------------------
    # 2. GET /api/orders/<id> -> api_get_order_detail
    # -------------------------------------------------------------------------
    @api.model
    def api_get_order_detail(self, order_id):
        """
        Fetch a single complete POS order with lines, payments, and full financials.
        """
        order = self.env['pos.order'].browse(int(order_id))
        if not order.exists():
            raise UserError(_("Order not found."))
        cids = self.env.context.get('allowed_company_ids', [])
        if cids and order.company_id.id not in cids:
            raise UserError(_("Order not found in this company."))

        # Debug: التحقق من المدفوعات
        payment_ids_db = order.payment_ids.ids
        print(f"=== DEBUG ORDER DETAIL: order_id={order_id}, payment_ids={payment_ids_db}, state={order.state}, amount_paid={order.amount_paid}")

        # جلب بنود الطلب بالحقول المطلوبة تماماً
        lines = [{
            'id': line.id,
            'product_id': [line.product_id.id, line.product_id.display_name] if line.product_id else False,
            'qty': line.qty,
            'price_unit': line.price_unit,
            'price_subtotal': line.price_subtotal,
            'discount': line.discount,
        } for line in order.lines]

        # جلب تفاصيل حركات الدفع والـ payment_status للـ POS
        payments = [{
            'id': pay.id,
            'payment_method_id': [pay.payment_method_id.id, pay.payment_method_id.name] if pay.payment_method_id else False,
            'amount': pay.amount,
            'payment_date': pay.payment_date.isoformat() if pay.payment_date else None,
            'payment_status': order.state, # ترث الدفعة حالة ترحيل الطلب الأساسي في الـ POS (paid, done)
        } for pay in order.payment_ids]

        # كائن ملخص الجلسة الاختياري للاستخدام عند الحاجة
        session_summary = False
        if order.session_id:
            session = order.session_id
            session_summary = {
                'session_id': session.id,
                'name': session.name,
                'state': session.state,
                'start_at': fields.Datetime.to_string(session.start_at) if session.start_at else False,
                'stop_at': fields.Datetime.to_string(session.stop_at) if session.stop_at else False,
                'user_name': session.user_id.name if session.user_id else False,
                'config_name': session.config_id.name if session.config_id else False,
                'financials': {
                    'total_orders_count': session.order_count,
                    'cash_register_balance_start': float(session.cash_register_balance_start or 0.0),
                    'cash_register_balance_end': float(session.cash_register_balance_end or 0.0),
                }
            }

        return {
            'id': order.id,
            'name': order.pos_reference or order.name,
            'date_order': order.date_order.isoformat() if order.date_order else None,
            'state': order.state,
            'partner_id': [order.partner_id.id, order.partner_id.name] if order.partner_id else False,
            'user_id': [order.user_id.id, order.user_id.name] if order.user_id else False,
            'session_id': [order.session_id.id, order.session_id.name] if order.session_id else False,
            'amount_total': order.amount_total,
            'amount_paid': order.amount_paid,
            'amount_tax': order.amount_tax,
            'amount_return': order.amount_return,
            'lines': lines,
            'payments': payments,
            'session_summary': session_summary,
        }
        
    @api.model
    def api_remove_order_line(self, *args, **kwargs):
        """
        حذف بند (Line) من الطلب بشكل كامل ومباشر عبر الـ SQL لتخطي حماية أودو للطلبات المدفوعة.
        يقوم تلقائياً بإعادة حساب الإجماليات والضرائب للطلب بعد الحذف.
        """
        order_id = kwargs.get('order_id') or (args[0] if len(args) > 0 else None)
        line_id  = kwargs.get('line_id')  or (args[1] if len(args) > 1 else None)

        if not order_id or not line_id:
            return {'status': 'error', 'message': 'Missing order_id or line_id parameters'}

        try:
            order = self.env['pos.order'].browse(int(order_id))
            line = self.env['pos.order.line'].browse(int(line_id))

            if not order.exists():
                return {'status': 'error', 'message': 'Order not found'}
            cids = self.env.context.get('allowed_company_ids', [])
            if cids and order.company_id.id not in cids:
                return {'status': 'error', 'message': 'Order not found in this company'}
            if not line.exists() or line.order_id.id != order.id:
                return {'status': 'error', 'message': 'Line not found or does not belong to this order'}

            # 💡 تخطي الأمان: نقوم بحذف البند مباشرة من جدول قاعدة البيانات (حتى لو كان الطلب paid أو done)
            self.env.cr.execute("DELETE FROM pos_order_line WHERE id = %s", (line.id,))
            
            # تحديث الذاكرة المؤقتة لأودو لقراءة البنود المتبقية فقط
            order.invalidate_recordset(['lines'])

            # إعادة حساب الإجماليات والضرائب بناءً على البنود المتبقية في الطلب
            remaining_lines = order.lines
            new_amount_total = sum(l.price_subtotal_incl for l in remaining_lines)
            new_amount_tax = sum(l.price_subtotal_incl - l.price_subtotal for l in remaining_lines)

            # تحديث رأس الطلب بالإجماليات الجديدة مباشرة عبر الـ SQL
            self.env.cr.execute("""
                UPDATE pos_order 
                SET amount_total = %s, amount_tax = %s
                WHERE id = %s
            """, (new_amount_total, new_amount_tax, order.id))

            # تحديث الذاكرة المؤقتة لرأس الطلب
            order.invalidate_recordset(['amount_total', 'amount_tax'])

            # حفظ وتطبيق التعديلات فوراً
            self.env.cr.flush()

            return {
                'status': 'success',
                'message': 'Line removed successfully via database layer',
                'amount_total': new_amount_total,
                'amount_tax': new_amount_tax
            }
        except Exception as e:
            print(f"!!! CRITICAL: Failed to remove order line via SQL: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    @api.model
    def api_register_order_payments(self, *args, **kwargs):
        """
        تسجيل المدفوعات من خلال معالجة مصفوفة الدفع الكاملة القادمة من الـ Frontend.
        """
        # تفكيك البيانات القادمة من الـ RPC بأي شكل كانت
        if args and isinstance(args[0], dict):
            kwargs.update(args[0])
        elif args and len(args) == 1 and isinstance(args[0], list) and isinstance(args[0][0], dict):
            kwargs.update(args[0][0])
            
        if 'params' in kwargs and isinstance(kwargs['params'], dict):
            kwargs.update(kwargs['params'])

        # 1. التقاط الـ order_id
        order_id = kwargs.get('order_id') or kwargs.get('orderId') or kwargs.get('id')
        if not order_id:
            return {'status': 'error', 'message': 'Missing order_id parameter.'}

        order = self.env['pos.order'].browse(int(order_id))
        if not order.exists():
            return {'status': 'error', 'message': 'Order not found.'}
        cids = self.env.context.get('allowed_company_ids', [])
        if cids and order.company_id.id not in cids:
            return {'status': 'error', 'message': 'Order not found in this company'}

        # 2. جلب مصفوفة المدفوعات المرسلة من الجافا سكريبت
        payments_list = kwargs.get('payments', [])
        if not payments_list and 'params' in kwargs:
            payments_list = kwargs['params'].get('payments', [])

        if not isinstance(payments_list, list):
            return {'status': 'error', 'message': 'Payments parameter must be a list.'}

        try:
            order_field_name = 'pos_order_id' if 'pos_order_id' in self.env['pos.payment']._fields else 'order_id'
            user_id = self.env.uid or order.user_id.id or 1
            current_time = fields.Datetime.now()

            print(f"=== PAYMENT DEBUG: order_id={order_id}, order_field_name={order_field_name}")
            print(f"=== PAYMENT DEBUG: incoming payments_list={payments_list}")

            # 3. جمع IDs الدفعات القادمة من الـ Frontend
            incoming_ids = []
            for pay_data in payments_list:
                pay_id = pay_data.get('id')
                if pay_id is not None and str(pay_id).strip().lower() not in ('null', 'none', ''):
                    incoming_ids.append(int(pay_id))
            print(f"=== PAYMENT DEBUG: incoming_ids={incoming_ids}")

            # 4. حذف الدفعات الموجودة في قاعدة البيانات وغير الموجودة في القائمة القادمة
            self.env.cr.execute(f"SELECT id, payment_method_id, amount FROM pos_payment WHERE {order_field_name} = %s", (order.id,))
            existing_rows = self.env.cr.fetchall()
            existing_ids = [row[0] for row in existing_rows]
            print(f"=== PAYMENT DEBUG: existing payments={existing_rows}")
            ids_to_delete = [pid for pid in existing_ids if pid not in incoming_ids]
            print(f"=== PAYMENT DEBUG: ids_to_delete={ids_to_delete}")
            if ids_to_delete:
                self.env.cr.execute("DELETE FROM pos_payment WHERE id IN %s", (tuple(ids_to_delete),))
                print(f"=== PAYMENT DEBUG: deleted ids={ids_to_delete}")

            # 5. معالجة كل دفعة: تحديث الموجودة / إضافة الجديدة
            for pay_data in payments_list:
                pay_id = pay_data.get('id')
                method_id = pay_data.get('method_id') or pay_data.get('methodId')
                amount = float(pay_data.get('amount', 0.0))

                if not method_id or int(method_id) <= 0:
                    print(f"=== PAYMENT DEBUG: SKIP (invalid method_id={method_id}): {pay_data}")
                    continue
                if amount <= 0.0:
                    print(f"=== PAYMENT DEBUG: SKIP (invalid amount={amount}): {pay_data}")
                    continue

                is_existing = (
                    pay_id is not None
                    and str(pay_id).strip().lower() not in ('null', 'none', '')
                )

                if is_existing:
                    print(f"=== PAYMENT DEBUG: UPDATE payment id={pay_id} amount={amount}")
                    self.env.cr.execute(
                        f"UPDATE pos_payment SET amount = %s WHERE id = %s AND {order_field_name} = %s",
                        (amount, int(pay_id), order.id),
                    )
                else:
                    print(f"=== PAYMENT DEBUG: INSERT payment method_id={method_id} amount={amount} order_id={order.id} field={order_field_name}")
                    self.env.cr.execute(
                        f"""
                        INSERT INTO pos_payment (payment_method_id, amount, payment_date, {order_field_name}, session_id, company_id, create_uid, write_uid, create_date, write_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (int(method_id), amount, current_time, order.id, order.session_id.id, order.company_id.id, user_id, user_id, current_time, current_time),
                    )

            # 6. إعادة حساب amount_paid دائماً باستخدام SQL مباشر
            self.env.cr.execute("SELECT COALESCE(SUM(amount), 0.0) FROM pos_payment WHERE %s = %%s" % order_field_name, (order.id,))
            new_total_paid = self.env.cr.fetchone()[0]
            print(f"=== PAYMENT DEBUG: new_total_paid={new_total_paid}, amount_total={order.amount_total}")

            if new_total_paid >= order.amount_total:
                self.env.cr.execute("""
                    UPDATE pos_order
                    SET amount_paid = %s, state = 'paid'
                    WHERE id = %s
                """, (new_total_paid, order.id))
            else:
                self.env.cr.execute("""
                    UPDATE pos_order
                    SET amount_paid = %s
                    WHERE id = %s
                """, (new_total_paid, order.id))

            self.env.cr.commit()

            # Verify after save
            self.env.cr.execute(f"SELECT id, payment_method_id, amount FROM pos_payment WHERE {order_field_name} = %s", (order.id,))
            verify_rows = self.env.cr.fetchall()
            print(f"=== PAYMENT DEBUG: after save payments={verify_rows}")

            return {
                'status': 'success',
                'message': f'Processed successfully. Payments synced.',
                'amount_paid': new_total_paid,
                'state': 'paid' if new_total_paid >= order.amount_total else order.state,
            }

        except Exception as e:
            print(f"!!! CRITICAL ARRAY PAYMENT ERROR: {str(e)}")
            return {'status': 'error', 'message': f'Database processing failed: {str(e)}'}

    @api.model
    def api_add_payment(self, *args, **kwargs):
        """
        إضافة حركة دفع لطلب في مرحلة المسودة وتغيير حالته إذا تم استيفاء المبلغ بالكامل.
        """
        order_id          = kwargs.get('order_id')          or (args[0] if len(args) > 0 else None)
        payment_method_id = kwargs.get('payment_method_id') or (args[1] if len(args) > 1 else None)
        amount            = kwargs.get('amount')            or (args[2] if len(args) > 2 else 0.0)

        if not order_id or not payment_method_id:
            return {'status': 'error', 'message': 'Missing required payment parameters.'}

        try:
            order = self.env['pos.order'].browse(int(order_id))
            if not order.exists():
                return {'status': 'error', 'message': 'Order not found'}
            cids = self.env.context.get('allowed_company_ids', [])
            if cids and order.company_id.id not in cids:
                return {'status': 'error', 'message': 'Order not found in this company'}

            # إنشاء الدفعة
            payment_vals = {
                'order_id': order.id,
                'payment_method_id': int(payment_method_id),
                'amount': float(amount),
                'payment_date': fields.Datetime.now(),
            }
            self.env['pos.payment'].sudo().create(payment_vals)

            # تحديث حالة الدفع للطلب وتأكيده إذا اكتمل المبلغ
            if order.amount_paid >= order.amount_total:
                order.action_pos_order_paid()

            return {
                'status': 'success',
                'message': 'Payment added successfully',
                'amount_paid': order.amount_paid,
                'amount_return': order.amount_return,
                'state': order.state
            }
        except Exception as e:
            _logger.error(f"Error in api_add_payment: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    @api.model
    def api_update_order_status(self, order_id, new_status):
        """
        تحديث حالة طلب نقطة البيع بشكل مباشر عبر استعلامات SQL لتخطي حماية أودو الصارمة للطلبات المدفوعة.
        """
        order = self.env['pos.order'].browse(int(order_id))
        if not order.exists():
            raise UserError(_("Order not found."))
        cids = self.env.context.get('allowed_company_ids', [])
        if cids and order.company_id.id not in cids:
            raise UserError(_("Order not found in this company."))

        # تنظيف وتجهيز الحالة المرسلة
        cleaned_status = str(new_status).strip().lower()
        if cleaned_status == 'cancelled':
            cleaned_status = 'cancel'

        # مصفوفة الحالات المدعومة في نظام أودو القياسي لنقاط البيع
        valid_states = ['draft', 'cancel', 'paid', 'done', 'invoiced']
        
        if cleaned_status not in valid_states:
            raise UserError(_("Invalid status '%s'. Valid states are: draft, cancel, paid, done, invoiced") % new_status)

        try:
            # 💡 الحل العبقري: تحديث الحالة مباشرة في جدول قاعدة البيانات لتخطي الـ ORM UserError والـ Restrictions
            self.env.cr.execute("""
                UPDATE pos_order 
                SET state = %s 
                WHERE id = %s
            """, (cleaned_status, order.id))
            
            # عمل تحديث للذاكرة المؤقتة (Invalidate Cache) لكي يقرأ أودو الحالة الجديدة فوراً
            order.invalidate_recordset(['state'])

            # إذا تم إلغاء الطلب، يفضل محاسبياً مسح أو إلغاء قيود الحركات المرتبطة به إن وجدت لتجنب المشاكل المالية
            if cleaned_status == 'cancel':
                # إلغاء ربط الحركات المخزنية المفتوحة (إن وجدت ولم تكن مرحلة بشكل نهائي)
                if hasattr(order, 'picking_ids') and order.picking_ids:
                    self.env.cr.execute("""
                        UPDATE stock_picking 
                        SET state = 'cancel' 
                        WHERE id IN %s AND state NOT IN ('done', 'cancel')
                    """, (tuple(order.picking_ids.ids),))

            # حفظ التعديلات بشكل نهائي في الجلسة الحالية
            self.env.cr.flush()

            return {
                'status': 'success', 
                'message': f'Order status updated successfully via SQL layer to: {cleaned_status}',
                'new_state': cleaned_status
            }

        except Exception as e:
            # تم استخدام طباعة النظام الافتراضية هنا بدلاً من الـ _logger لتفادي الـ NameError تماماً
            print(f"!!! CRITICAL: Failed to update order status via SQL layer: {str(e)}")
            return {
                'status': 'error', 
                'message': f'Database execution failed: {str(e)}'
            }