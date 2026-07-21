import json
import uuid
import logging
from datetime import datetime
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

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
            _logger.debug(
                "GET_ORDERS ITEM | order_id=%s | name=%s | state=%s | amount_total=%s | amount_paid=%s | partner_id=%s",
                order.id, order.pos_reference, order.state,
                order.amount_total, order.amount_paid,
                order.partner_id.id if order.partner_id else None,
            )
            orders_data.append({
                'id': order.id,
                'name': order.pos_reference or order.name,
                'date_order': order.date_order.isoformat() if order.date_order else False,
                'state': order.state,
                'partner_id': [order.partner_id.id, order.partner_id.name] if order.partner_id else False,
                'user_id': [order.user_id.id, order.user_id.name] if order.user_id else False,
                'session_id': [order.session_id.id, order.session_id.name] if order.session_id else False,
                'amount_total': order.amount_total,
                'amount_paid': order.amount_paid,
                'amount_tax': order.amount_tax,
                'amount_return': order.amount_return,
                'amount_discount': order.amount_total - sum(l.price_subtotal for l in order.lines),
                'order_discount': order.order_discount or 0.0,
                'order_discount_type': order.order_discount_type or 'fixed',
                'service_fee': order.service_fee or 0.0,
                'service_fee_type': order.service_fee_type or 'fixed',
                'note': order.general_note or '',
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
            'payment_date': pay.payment_date.isoformat() if pay.payment_date else False,
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

        _logger.info(
            "GET_ORDER_DETAIL | order_id=%s | state=%s | amount_total=%s | amount_paid=%s | "
            "order_discount=%s | order_discount_type=%s | service_fee=%s | service_fee_type=%s | note=%s",
            order.id, order.state, order.amount_total, order.amount_paid,
            order.order_discount, order.order_discount_type,
            order.service_fee, order.service_fee_type,
            order.general_note,
        )

        return {
            'id': order.id,
            'name': order.pos_reference or order.name,
            'date_order': order.date_order.isoformat() if order.date_order else False,
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
            'order_discount': order.order_discount or 0.0,
            'order_discount_type': order.order_discount_type or 'fixed',
            'service_fee': order.service_fee or 0.0,
            'service_fee_type': order.service_fee_type or 'fixed',
            'note': order.general_note or '',
        }

    @api.model
    def api_remove_order_line(self, *args, **kwargs):
        """
        حذف بند (Line) من الطلب مع إعادة حساب الإجماليات والمبالغ المدفوعة.
        """
        order_id = kwargs.get('order_id') or (args[0] if len(args) > 0 else None)
        line_id  = kwargs.get('line_id')  or (args[1] if len(args) > 1 else None)

        if not order_id or not line_id:
            return {'status': 'error', 'message': 'Missing order_id or line_id parameters'}

        _logger.info(
            "MUTATION REMOVE_LINE | order_id=%s | line_id=%s | timestamp=%s",
            order_id, line_id, datetime.utcnow().isoformat(),
        )

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

            # Log existing state before mutation
            _logger.info(
                "MUTATION REMOVE_LINE BEFORE | order_id=%s | amount_total=%s | amount_paid=%s | amount_tax=%s",
                order_id, order.amount_total, order.amount_paid, order.amount_tax,
            )

            # Direct SQL delete
            self.env.cr.execute("DELETE FROM pos_order_line WHERE id = %s", (line.id,))

            # Invalidate ALL cache to force fresh reads
            self.env.invalidate_all()

            # Recalculate totals via SQL (avoid ORM cached lines)
            self.env.cr.execute("""
                SELECT COALESCE(SUM(price_subtotal_incl), 0),
                       COALESCE(SUM(price_subtotal_incl - price_subtotal), 0)
                FROM pos_order_line
                WHERE order_id = %s
            """, (order.id,))
            row = self.env.cr.fetchone()
            new_amount_total = float(row[0])
            new_amount_tax = float(row[1])

            # Recalculate amount_paid: it should NOT exceed new_total
            self.env.cr.execute(
                "SELECT COALESCE(SUM(amount), 0.0) FROM pos_payment WHERE pos_order_id = %s",
                (order.id,)
            )
            payments_sum = float(self.env.cr.fetchone()[0])
            new_amount_paid = min(payments_sum, new_amount_total)

            # Update order header
            self.env.cr.execute("""
                UPDATE pos_order 
                SET amount_total = %s, amount_tax = %s, amount_paid = %s
                WHERE id = %s
            """, (new_amount_total, new_amount_tax, new_amount_paid, order.id))

            self.env.invalidate_all()
            self.env.cr.commit()

            _logger.info(
                "MUTATION REMOVE_LINE AFTER | order_id=%s | new_amount_total=%s | new_amount_paid=%s | new_amount_tax=%s",
                order_id, new_amount_total, new_amount_paid, new_amount_tax,
            )

            return {
                'status': 'success',
                'message': 'Line removed successfully',
                'amount_total': new_amount_total,
                'amount_tax': new_amount_tax,
                'amount_paid': new_amount_paid,
            }
        except Exception as e:
            _logger.error("MUTATION REMOVE_LINE ERROR | order_id=%s | error=%s", order_id, str(e))
            return {'status': 'error', 'message': str(e)}

    @api.model
    def api_register_order_payments(self, *args, **kwargs):
        """
        تسجيل المدفوعات مع إعادة حساب amount_paid وإلغاء الذاكرة المؤقتة.
        """
        if args and isinstance(args[0], dict):
            kwargs.update(args[0])
        elif args and len(args) == 1 and isinstance(args[0], list) and isinstance(args[0][0], dict):
            kwargs.update(args[0][0])
            
        if 'params' in kwargs and isinstance(kwargs['params'], dict):
            kwargs.update(kwargs['params'])

        order_id = kwargs.get('order_id') or kwargs.get('orderId') or kwargs.get('id')
        if not order_id:
            return {'status': 'error', 'message': 'Missing order_id parameter.'}

        order = self.env['pos.order'].browse(int(order_id))
        if not order.exists():
            return {'status': 'error', 'message': 'Order not found.'}
        cids = self.env.context.get('allowed_company_ids', [])
        if cids and order.company_id.id not in cids:
            return {'status': 'error', 'message': 'Order not found in this company'}

        payments_list = kwargs.get('payments', [])
        if not payments_list and 'params' in kwargs:
            payments_list = kwargs['params'].get('payments', [])

        if not isinstance(payments_list, list):
            return {'status': 'error', 'message': 'Payments parameter must be a list.'}

        _logger.info(
            "MUTATION REGISTER_PAYMENTS | order_id=%s | payment_count=%s | payments=%s | timestamp=%s",
            order_id, len(payments_list),
            json.dumps(payments_list, default=str),
            datetime.utcnow().isoformat(),
        )

        try:
            order_field_name = 'pos_order_id' if 'pos_order_id' in self.env['pos.payment']._fields else 'order_id'
            user_id = self.env.uid or order.user_id.id or 1
            current_time = fields.Datetime.now()

            incoming_ids = []
            for pay_data in payments_list:
                pay_id = pay_data.get('id')
                if pay_id is not None and str(pay_id).strip().lower() not in ('null', 'none', ''):
                    incoming_ids.append(int(pay_id))

            self.env.cr.execute(f"SELECT id, payment_method_id, amount FROM pos_payment WHERE {order_field_name} = %s", (order.id,))
            existing_rows = self.env.cr.fetchall()
            existing_ids = [row[0] for row in existing_rows]
            ids_to_delete = [pid for pid in existing_ids if pid not in incoming_ids]
            if ids_to_delete:
                self.env.cr.execute("DELETE FROM pos_payment WHERE id IN %s", (tuple(ids_to_delete),))
                _logger.info("MUTATION REGISTER_PAYMENTS DELETE | order_id=%s | deleted_ids=%s", order_id, ids_to_delete)

            for pay_data in payments_list:
                pay_id = pay_data.get('id')
                method_id = pay_data.get('method_id') or pay_data.get('methodId')
                amount = float(pay_data.get('amount', 0.0))

                if not method_id or int(method_id) <= 0:
                    continue
                if amount <= 0.0:
                    continue

                is_existing = (
                    pay_id is not None
                    and str(pay_id).strip().lower() not in ('null', 'none', '')
                )

                if is_existing:
                    self.env.cr.execute(
                        f"UPDATE pos_payment SET amount = %s WHERE id = %s AND {order_field_name} = %s",
                        (amount, int(pay_id), order.id),
                    )
                else:
                    self.env.cr.execute(
                        f"""
                        INSERT INTO pos_payment (payment_method_id, amount, payment_date, {order_field_name}, session_id, company_id, create_uid, write_uid, create_date, write_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (int(method_id), amount, current_time, order.id, order.session_id.id, order.company_id.id, user_id, user_id, current_time, current_time),
                    )

            # Recalculate amount_paid from actual payments via SQL
            self.env.cr.execute(
                "SELECT COALESCE(SUM(amount), 0.0) FROM pos_payment WHERE %s = %%s" % order_field_name,
                (order.id,)
            )
            new_total_paid = float(self.env.cr.fetchone()[0])

            # Read latest amount_total from DB (not ORM cache)
            self.env.cr.execute(
                "SELECT amount_total FROM pos_order WHERE id = %s", (order.id,)
            )
            db_amount_total = float(self.env.cr.fetchone()[0])

            _logger.info(
                "MUTATION REGISTER_PAYMENTS CALC | order_id=%s | db_amount_total=%s | new_total_paid=%s",
                order_id, db_amount_total, new_total_paid,
            )

            if new_total_paid >= db_amount_total:
                self.env.cr.execute("""
                    UPDATE pos_order
                    SET amount_paid = %s, state = 'paid'
                    WHERE id = %s
                """, (new_total_paid, order.id))
                new_state = 'paid'
            else:
                self.env.cr.execute("""
                    UPDATE pos_order
                    SET amount_paid = %s
                    WHERE id = %s
                """, (new_total_paid, order.id))
                new_state = order.state

            self.env.cr.commit()

            # Invalidate all ORM cache for this record
            self.env.invalidate_all()

            _logger.info(
                "MUTATION REGISTER_PAYMENTS SUCCESS | order_id=%s | amount_paid=%s | state=%s",
                order_id, new_total_paid, new_state,
            )

            return {
                'status': 'success',
                'message': 'Payments synced successfully.',
                'amount_paid': new_total_paid,
                'state': new_state,
            }

        except Exception as e:
            _logger.error("MUTATION REGISTER_PAYMENTS ERROR | order_id=%s | error=%s", order_id, str(e))
            return {'status': 'error', 'message': f'Database processing failed: {str(e)}'}

    @api.model
    def api_add_payment(self, *args, **kwargs):
        """
        إضافة حركة دفع مع إعادة حساب amount_paid مباشرة وإلغاء الذاكرة المؤقتة.
        """
        order_id          = kwargs.get('order_id')          or (args[0] if len(args) > 0 else None)
        payment_method_id = kwargs.get('payment_method_id') or (args[1] if len(args) > 1 else None)
        amount            = kwargs.get('amount')            or (args[2] if len(args) > 2 else 0.0)

        if not order_id or not payment_method_id:
            return {'status': 'error', 'message': 'Missing required payment parameters.'}

        _logger.info(
            "MUTATION ADD_PAYMENT | order_id=%s | method_id=%s | amount=%s | timestamp=%s",
            order_id, payment_method_id, amount, datetime.utcnow().isoformat(),
        )

        try:
            order = self.env['pos.order'].browse(int(order_id))
            if not order.exists():
                return {'status': 'error', 'message': 'Order not found'}
            cids = self.env.context.get('allowed_company_ids', [])
            if cids and order.company_id.id not in cids:
                return {'status': 'error', 'message': 'Order not found in this company'}

            new_payment = self.env['pos.payment'].sudo().create({
                'pos_order_id': order.id,
                'payment_method_id': int(payment_method_id),
                'amount': float(amount),
                'payment_date': fields.Datetime.now(),
            })

            # Invalidate to clear cached amount_paid / amount_total
            self.env.invalidate_all()

            # Recalculate amount_paid from SQL
            self.env.cr.execute(
                "SELECT COALESCE(SUM(amount), 0.0) FROM pos_payment WHERE pos_order_id = %s",
                (order.id,)
            )
            total_paid = float(self.env.cr.fetchone()[0])

            # Read actual amount_total from DB
            self.env.cr.execute(
                "SELECT amount_total FROM pos_order WHERE id = %s", (order.id,)
            )
            db_total = float(self.env.cr.fetchone()[0])

            if total_paid >= db_total:
                self.env.cr.execute("""
                    UPDATE pos_order SET amount_paid = %s, state = 'paid' WHERE id = %s
                """, (total_paid, order.id))
                new_state = 'paid'
            else:
                self.env.cr.execute("""
                    UPDATE pos_order SET amount_paid = %s WHERE id = %s
                """, (total_paid, order.id))
                new_state = order.state

            self.env.cr.commit()
            self.env.invalidate_all()

            _logger.info(
                "MUTATION ADD_PAYMENT SUCCESS | order_id=%s | total_paid=%s | state=%s",
                order_id, total_paid, new_state,
            )

            return {
                'status': 'success',
                'message': 'Payment added successfully',
                'amount_paid': total_paid,
                'amount_return': 0.0,
                'state': new_state,
            }
        except Exception as e:
            _logger.error("MUTATION ADD_PAYMENT ERROR | order_id=%s | error=%s", order_id, str(e))
            return {'status': 'error', 'message': str(e)}

    @api.model
    def _revert_done_picking(self, picking, order_name):
        """Create a return picking to revert stock for a done stock picking.
        Handles lot/serial tracked products by copying move line details.
        Returns True on success, False if skipped or failed.
        """
        if picking.state != 'done':
            picking.action_cancel()
            return True

        return_picking_type = picking.picking_type_id.sudo().return_picking_type_id or picking.picking_type_id
        return_picking = self.env['stock.picking'].create({
            'location_id': picking.location_dest_id.id,
            'location_dest_id': picking.location_id.id,
            'picking_type_id': return_picking_type.id,
            'partner_id': picking.partner_id.id,
            'origin': _('Cancel: %s', order_name),
            'move_type': 'direct',
            'state': 'draft',
        })
        for move in picking.move_ids.filtered(lambda m: m.state == 'done'):
            reverse_move = self.env['stock.move'].create({
                'name': _('Cancel: %s', move.name),
                'product_id': move.product_id.id,
                'product_uom_qty': move.product_uom_qty,
                'product_uom': move.product_uom.id,
                'location_id': move.location_dest_id.id,
                'location_dest_id': move.location_id.id,
                'picking_id': return_picking.id,
                'picking_type_id': return_picking_type.id,
                'state': 'draft',
            })
            for mline in move.move_line_ids:
                self.env['stock.move.line'].create({
                    'move_id': reverse_move.id,
                    'picking_id': return_picking.id,
                    'product_id': mline.product_id.id,
                    'product_uom_id': mline.product_uom_id.id,
                    'quantity': mline.quantity,
                    'location_id': mline.location_dest_id.id,
                    'location_dest_id': mline.location_id.id,
                    'company_id': mline.company_id.id,
                    'lot_id': mline.lot_id.id,
                    'lot_name': mline.lot_name,
                    'package_id': mline.package_id.id,
                    'result_package_id': mline.result_package_id.id,
                    'owner_id': mline.owner_id.id,
                })
            reverse_move.write({
                'quantity': reverse_move.product_uom_qty,
                'picked': True,
            })
        return_picking.action_confirm()
        try:
            with self.env.cr.savepoint():
                return_picking._action_done()
            picking.write({'state': 'cancel'})
            return True
        except (UserError, ValidationError):
            return False

    @api.model
    def api_update_order_status(self, order_id, new_status):
        """
        تحديث حالة طلب نقطة البيع بشكل مباشر مع مزامنة amount_paid
        """
        _logger.info(
            "MUTATION UPDATE_STATUS | order_id=%s | new_status=%s | timestamp=%s",
            order_id, new_status, datetime.utcnow().isoformat(),
        )
        order = self.env['pos.order'].browse(int(order_id))
        if not order.exists():
            raise UserError(_("Order not found."))
        cids = self.env.context.get('allowed_company_ids', [])
        if cids and order.company_id.id not in cids:
            raise UserError(_("Order not found in this company."))

        cleaned_status = str(new_status).strip().lower()
        if cleaned_status == 'cancelled':
            cleaned_status = 'cancel'

        valid_states = ['draft', 'cancel', 'paid', 'done', 'invoiced']
        
        if cleaned_status not in valid_states:
            raise UserError(_("Invalid status '%s'. Valid states are: draft, cancel, paid, done, invoiced") % new_status)

        try:
            # Update state and sync amount_paid if transitioning to 'paid'
            if cleaned_status == 'paid':
                # Fetch actual sum of payments via SQL
                self.env.cr.execute(
                    "SELECT COALESCE(SUM(amount), 0.0) FROM pos_payment WHERE pos_order_id = %s",
                    (order.id,)
                )
                total_paid = float(self.env.cr.fetchone()[0])
                self.env.cr.execute("""
                    UPDATE pos_order
                    SET state = %s, amount_paid = %s
                    WHERE id = %s
                """, (cleaned_status, total_paid, order.id))
                _logger.info(
                    "MUTATION UPDATE_STATUS PAID | order_id=%s | amount_paid=%s | amount_total=%s",
                    order_id, total_paid, order.amount_total,
                )
            else:
                self.env.cr.execute("""
                    UPDATE pos_order 
                    SET state = %s 
                    WHERE id = %s
                """, (cleaned_status, order.id))

            # Invalidate ALL ORM cache for this record to prevent stale reads
            self.env.invalidate_all()

            # Cancel related stock pickings + revert stock quantities
            if cleaned_status == 'cancel':
                if hasattr(order, 'picking_ids') and order.picking_ids:
                    for picking in order.picking_ids:
                        self._revert_done_picking(picking, order.name)

            self.env.cr.commit()

            _logger.info(
                "MUTATION UPDATE_STATUS SUCCESS | order_id=%s | new_state=%s",
                order_id, cleaned_status,
            )

            return {
                'status': 'success', 
                'message': f'Order status updated to: {cleaned_status}',
                'new_state': cleaned_status,
                'amount_paid': total_paid if cleaned_status == 'paid' else order.amount_paid,
            }

        except Exception as e:
            _logger.error("MUTATION UPDATE_STATUS ERROR | order_id=%s | error=%s", order_id, str(e))
            return {
                'status': 'error', 
                'message': f'Database execution failed: {str(e)}'
            }

    @api.model
    def api_fix_cancelled_order_stock(self, order_id=None):
        """
        Retroactively revert stock for cancelled orders whose quantities
        weren't reverted (due to old bug before the fix).
        If order_id given, fix only that order. Otherwise fix all cancelled orders.
        Call via RPC: odoo.execute_kw('custom.order.api', 'api_fix_cancelled_order_stock', [{order_id: 123}])
        """
        domain = [('state', '=', 'cancel')]
        if order_id:
            domain.append(('id', '=', int(order_id)))

        orders = self.env['pos.order'].search(domain)
        fixed = 0
        skipped = 0

        for order in orders:
            if not hasattr(order, 'picking_ids') or not order.picking_ids:
                skipped += 1
                continue
            done_pickings = order.picking_ids.filtered(lambda p: p.state == 'done')
            if not done_pickings:
                skipped += 1
                continue

            any_fixed = False
            for picking in done_pickings:
                if self._revert_done_picking(picking, order.name):
                    any_fixed = True
            if any_fixed:
                fixed += 1
            else:
                skipped += 1

        return {
            'status': 'success',
            'fixed': fixed,
            'skipped': skipped,
        }

    @api.model
    def api_update_order(self, *args, **kwargs):
        """
        Update an existing POS order: lines, discount, service fee, customer, notes.
        Recalculates totals after change. Only draft orders can be edited.
        """
        if args and isinstance(args[0], dict):
            kwargs.update(args[0])
        elif args and len(args) == 1 and isinstance(args[0], list) and isinstance(args[0][0], dict):
            kwargs.update(args[0][0])

        if 'params' in kwargs and isinstance(kwargs['params'], dict):
            kwargs.update(kwargs['params'])

        order_id = kwargs.get('order_id') or kwargs.get('orderId') or kwargs.get('id')
        if not order_id:
            return {'status': 'error', 'message': 'Missing order_id parameter.'}

        order = self.env['pos.order'].browse(int(order_id))
        if not order.exists():
            return {'status': 'error', 'message': 'Order not found.'}
        cids = self.env.context.get('allowed_company_ids', [])
        if cids and order.company_id.id not in cids:
            return {'status': 'error', 'message': 'Order not found in this company.'}

        # ---- Log original state ----
        _logger.info(
            "MUTATION UPDATE_ORDER BEGIN | order_id=%s | state=%s | "
            "amount_total=%s | amount_paid=%s | amount_tax=%s | "
            "partner_id=%s | general_note=%s | "
            "order_discount=%s | order_discount_type=%s | service_fee=%s | service_fee_type=%s",
            order.id, order.state,
            order.amount_total, order.amount_paid, order.amount_tax,
            order.partner_id.id if order.partner_id else None,
            order.general_note,
            order.order_discount, order.order_discount_type,
            order.service_fee, order.service_fee_type,
        )

        if order.state != 'draft':
            return {
                'status': 'error',
                'message': 'Cannot edit an order that is already paid or completed. Only draft orders can be edited.',
            }

        items = kwargs.get('items', [])
        order_discount = float(kwargs.get('order_discount', 0) or 0)
        order_discount_type = str(kwargs.get('order_discount_type', 'fixed') or 'fixed')
        service_fee = float(kwargs.get('service_fee', 0) or 0)
        service_fee_type = str(kwargs.get('service_fee_type', 'fixed') or 'fixed')
        customer_id = kwargs.get('customer_id')
        note = str(kwargs.get('note', '') or '')

        _logger.info(
            "MUTATION UPDATE_ORDER PAYLOAD | order_id=%s | "
            "order_discount=%s | order_discount_type=%s | service_fee=%s | service_fee_type=%s | "
            "customer_id=%s | note=%s | items_count=%s",
            order.id,
            order_discount, order_discount_type, service_fee, service_fee_type,
            customer_id, note, len(items),
        )
        _logger.debug("MUTATION UPDATE_ORDER ITEMS | order_id=%s | items=%s", order.id, json.dumps(items, default=str))

        try:
            # ---- 1. Update customer ----
            if customer_id is not None and customer_id is not False and str(customer_id).strip() not in ('', 'false', 'null'):
                partner_id = int(customer_id)
                partner = self.env['res.partner'].browse(partner_id)
                if partner.exists():
                    self.env.cr.execute(
                        "UPDATE pos_order SET partner_id = %s WHERE id = %s",
                        (partner.id, order.id),
                    )
                    _logger.info("MUTATION UPDATE_ORDER CUSTOMER | order_id=%s | partner_id=%s", order.id, partner.id)
                else:
                    _logger.warning("MUTATION UPDATE_ORDER CUSTOMER | order_id=%s | partner_id=%s NOT FOUND, clearing", order.id, customer_id)
                    self.env.cr.execute(
                        "UPDATE pos_order SET partner_id = NULL WHERE id = %s",
                        (order.id,),
                    )
            elif customer_id is False or customer_id is None:
                self.env.cr.execute(
                    "UPDATE pos_order SET partner_id = NULL WHERE id = %s",
                    (order.id,),
                )
                _logger.info("MUTATION UPDATE_ORDER CUSTOMER | order_id=%s | cleared", order.id)

            # ---- 2. Update note ----
            self.env.cr.execute(
                "UPDATE pos_order SET general_note = %s WHERE id = %s",
                (note, order.id),
            )
            _logger.info("MUTATION UPDATE_ORDER NOTE | order_id=%s | note=%s", order.id, note[:100] if note else '')

            # ---- 3. Update lines ----
            incoming_line_ids = []
            for item in items:
                line_id = item.get('line_id')
                if line_id and str(line_id).strip().lower() not in ('null', 'none', ''):
                    incoming_line_ids.append(int(line_id))

            # Fetch existing lines from DB
            self.env.cr.execute(
                "SELECT id FROM pos_order_line WHERE order_id = %s",
                (order.id,),
            )
            existing_line_rows = self.env.cr.fetchall()
            existing_line_ids = [r[0] for r in existing_line_rows]
            ids_to_delete = [lid for lid in existing_line_ids if lid not in incoming_line_ids]

            if ids_to_delete:
                self.env.cr.execute(
                    "DELETE FROM pos_order_line WHERE id IN %s",
                    (tuple(ids_to_delete),),
                )
                _logger.info("MUTATION UPDATE_ORDER DELETE_LINES | order_id=%s | deleted_ids=%s", order.id, ids_to_delete)

            for item in items:
                line_id = item.get('line_id')
                product_id = int(item.get('product_id', 0))
                qty = float(item.get('qty', 1) or 1)
                price = float(item.get('price', 0) or 0)
                discount = float(item.get('discount', 0) or 0)
                is_deleted = bool(item.get('_deleted', False))

                if is_deleted and line_id and str(line_id).strip().lower() not in ('null', 'none', ''):
                    self.env.cr.execute(
                        "DELETE FROM pos_order_line WHERE id = %s AND order_id = %s",
                        (int(line_id), order.id),
                    )
                    _logger.info("MUTATION UPDATE_ORDER DELETE_LINE | order_id=%s | line_id=%s", order.id, line_id)
                    continue

                product = self.env['product.product'].browse(product_id) if product_id else False
                if not product or not product.exists():
                    _logger.warning("MUTATION UPDATE_ORDER LINE_SKIP | order_id=%s | product_id=%s NOT FOUND", order.id, product_id)
                    continue

                # Calculate price_subtotal and price_subtotal_incl
                # For simplicity, use a standard tax rate or the product's tax
                taxes = product.taxes_id.filtered(lambda t: t.company_id.id == order.company_id.id)
                fpos = order.fiscal_position_id
                if fpos:
                    taxes = fpos.map_tax(taxes)
                price_subtotal = price * qty * (1 - discount / 100.0)
                # Compute tax-included subtotal
                if taxes:
                    tax_result = taxes.compute_all(price * (1 - discount / 100.0), order.currency_id, qty, product=product, partner=order.partner_id)
                    price_subtotal_incl = tax_result['total_included']
                else:
                    price_subtotal_incl = price_subtotal

                is_existing = (
                    line_id and str(line_id).strip().lower() not in ('null', 'none', '')
                    and int(line_id) in existing_line_ids
                )

                if is_existing:
                    self.env.cr.execute("""
                        UPDATE pos_order_line
                        SET qty = %s, price_unit = %s, discount = %s,
                            price_subtotal = %s, price_subtotal_incl = %s
                        WHERE id = %s AND order_id = %s
                    """, (qty, price, discount, price_subtotal, price_subtotal_incl, int(line_id), order.id))
                    _logger.debug(
                        "MUTATION UPDATE_ORDER UPDATE_LINE | order_id=%s | line_id=%s | qty=%s | price=%s | discount=%s",
                        order.id, line_id, qty, price, discount,
                    )
                else:
                    # Create new line
                    name = product.display_name or product.name
                    self.env.cr.execute("""
                        INSERT INTO pos_order_line
                            (order_id, product_id, name, qty, price_unit, discount,
                             price_subtotal, price_subtotal_incl, company_id, currency_id,
                             create_uid, write_uid, create_date, write_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        RETURNING id
                    """, (
                        order.id, product.id, name, qty, price, discount,
                        price_subtotal, price_subtotal_incl,
                        order.company_id.id, order.currency_id.id,
                        self.env.uid or 1, self.env.uid or 1,
                    ))
                    new_line_id = self.env.cr.fetchone()[0]
                    _logger.info(
                        "MUTATION UPDATE_ORDER CREATE_LINE | order_id=%s | new_line_id=%s | product_id=%s | qty=%s | price=%s",
                        order.id, new_line_id, product_id, qty, price,
                    )

            # ---- 4. Update order-level adjustments ----
            self.env.cr.execute("""
                UPDATE pos_order
                SET order_discount = %s,
                    order_discount_type = %s,
                    service_fee = %s,
                    service_fee_type = %s
                WHERE id = %s
            """, (order_discount, order_discount_type, service_fee, service_fee_type, order.id))

            # ---- 5. Recalculate totals ----
            self.env.invalidate_all()

            # Get current line totals from DB
            self.env.cr.execute("""
                SELECT COALESCE(SUM(price_subtotal_incl), 0),
                       COALESCE(SUM(price_subtotal_incl - price_subtotal), 0),
                       COALESCE(SUM(price_subtotal), 0)
                FROM pos_order_line
                WHERE order_id = %s
            """, (order.id,))
            row = self.env.cr.fetchone()
            total_incl = float(row[0])
            total_tax = float(row[1])
            total_excl = float(row[2])

            # Calculate discount amount
            if order_discount_type == 'percent':
                discount_amount = total_excl * (order_discount / 100.0)
            else:
                discount_amount = min(order_discount, total_incl)

            # Service fee (non-taxable as per your specification)
            if service_fee_type == 'percent':
                service_fee_amount = total_excl * (service_fee / 100.0)
            else:
                service_fee_amount = service_fee

            # Final total
            new_amount_total = max(0.0, total_incl - discount_amount + service_fee_amount)
            new_amount_tax = total_tax  # unchanged by discount/fee

            # Recalculate amount_paid: cap at new total
            self.env.cr.execute(
                "SELECT COALESCE(SUM(amount), 0.0) FROM pos_payment WHERE pos_order_id = %s",
                (order.id,),
            )
            payments_sum = float(self.env.cr.fetchone()[0])
            new_amount_paid = min(payments_sum, new_amount_total)

            _logger.info(
                "MUTATION UPDATE_ORDER RECALC | order_id=%s | "
                "total_incl=%s | discount_amount=%s | service_fee_amount=%s | "
                "new_amount_tax=%s | new_amount_total=%s | payments_sum=%s | new_amount_paid=%s",
                order.id,
                total_incl, discount_amount, service_fee_amount,
                new_amount_tax, new_amount_total, payments_sum, new_amount_paid,
            )

            # Write final totals
            self.env.cr.execute("""
                UPDATE pos_order
                SET amount_total = %s,
                    amount_tax = %s,
                    amount_paid = %s
                WHERE id = %s
            """, (new_amount_total, new_amount_tax, new_amount_paid, order.id))

            self.env.invalidate_all()
            self.env.cr.commit()

            _logger.info(
                "MUTATION UPDATE_ORDER SUCCESS | order_id=%s | "
                "amount_total=%s | amount_paid=%s | amount_tax=%s | "
                "order_discount=%s | service_fee=%s | partner_id=%s | note=%s",
                order.id, new_amount_total, new_amount_paid, new_amount_tax,
                order_discount, service_fee,
                customer_id, note,
            )

            return {
                'status': 'success',
                'message': 'Order updated successfully.',
                'amount_total': new_amount_total,
                'amount_paid': new_amount_paid,
                'amount_tax': new_amount_tax,
                'order_discount': order_discount,
                'service_fee': service_fee,
            }

        except Exception as e:
            _logger.error(
                "MUTATION UPDATE_ORDER ERROR | order_id=%s | error=%s",
                order_id, str(e),
                exc_info=True,
            )
            return {
                'status': 'error',
                'message': f'Database execution failed: {str(e)}',
            }