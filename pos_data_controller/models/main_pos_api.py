import uuid
import logging
from odoo import models, api, fields
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 1. PosConfig
# ─────────────────────────────────────────────────────────────────────────────
class PosConfig(models.Model):
    _inherit = 'pos.config'

    allow_out_of_stock_sale = fields.Boolean(
        string='Allow Sale of Out-of-Stock Products',
        default=False,
        help='If enabled, products with zero or negative stock can be sold in POS'
    )

    def _get_company_domain(self):
        """Return company filter domain from context, or empty."""
        cids = self.env.context.get('allowed_company_ids', [])
        if cids:
            return [('company_id', 'in', cids)]
        return []

    @api.model
    def _ensure_payment_methods_before_open(self, config_id_int):
        """
        تضمن هذه الدالة وجود 3 طرق دفع فريدة فقط (نقدي، بطاقة، حساب العميل) وربطها بالجهاز.
        تقوم بتنظيف قاعدة البيانات فورياً من أي طرق دفع كاش مكررة أو زائدة وتمنع ظهور 4 طرق دفع تماماً.
        """
        pos_config = self.browse(config_id_int)
        if not pos_config.exists():
            return None

        # 1. إذا كانت هناك جلسة نشطة، نكتفي بإرجاع الكاش المرتبط منعاً لأخطاء الحسابات
        active_session = self.env['pos.session'].search([
            ('config_id', '=', config_id_int),
            ('state', 'in', ['opened', 'closing_control', 'opening_control'])
        ], limit=1)
        
        if active_session:
            return pos_config.payment_method_ids.filtered(lambda pm: pm.is_cash_count)[:1]

        company_id  = pos_config.company_id.id
        config_name = pos_config.name

        # 2. جلب أو إنشاء دفاتر اليومية (Journals) بشكل آمن
        cash_journal = self.env['account.journal'].search([
            ('type', '=', 'cash'),
            ('company_id', '=', company_id)
        ], limit=1)

        if not cash_journal:
            sp = 'sp_create_cash_journal'
            self.env.cr.execute(f'SAVEPOINT "{sp}"')
            try:
                cash_journal = self.env['account.journal'].sudo().create({
                    'name':       f'نقدي - {config_name}',
                    'type':       'cash',
                    'company_id': company_id,
                    'code':       ('CSH%s' % config_id_int)[:5],
                })
                self.env.cr.execute(f'RELEASE SAVEPOINT "{sp}"')
            except Exception as e:
                self.env.cr.execute(f'ROLLBACK TO SAVEPOINT "{sp}"')
                _logger.error(f"Failed to create cash journal: {e}")
                cash_journal = False

        bank_journal = self.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', company_id)
        ], limit=1)

        # 3. إيجاد وتصفية طرق الدفع النقدية (الكاش) لمنع التكرار نهائياً
        # نبحث عن أي طريقة دفع كاش بالشركة سواء باسم "نقدي" أو "Cash" أو مرتبطة بدفتر كاش
        all_cash_methods = self.env['pos.payment.method'].search([
            ('company_id', '=', company_id),
            '|', '|', 
            ('journal_id.type', '=', 'cash'),
            ('name', '=ilike', 'نقدي%'),
            ('name', '=ilike', 'cash%')
        ])

        # نختار سجل واحد رئيسي ليكون هو الكاش المعتمد
        cash_method = all_cash_methods[:1]
        
        # ⚠️ خطوة الحماية المروعة: إذا كان هناك كاش مكرر وزائد (أكثر من 1)، نقوم بفصله وإزالته من الـ Config تماماً
        duplicated_cash_methods = charities = all_cash_methods[1:]
        if duplicated_cash_methods:
            _logger.info(f"Cleaning duplicated cash payment methods for company {company_id}: {duplicated_cash_methods.ids}")
            # نقوم بإزالتهم بشكل مباشر من إعدادات نقاط البيع عبر استعلام قاعدة البيانات لضمان اختفائهم
            self.env.cr.execute("""
                DELETE FROM pos_config_pos_payment_method_rel 
                WHERE pos_payment_method_id IN %s
            """, (tuple(duplicated_cash_methods.ids),))

        # 4. تعريف محدد ومحمي للثلاث طرق المطلوبة فقط
        required_methods = [
            {'key': 'cash',     'name': 'نقدي',         'journal_id': cash_journal.id if cash_journal else False, 'existing_record': cash_method},
            {'key': 'bank',     'name': 'بطاقة',        'journal_id': bank_journal.id if bank_journal else False, 'existing_record': False},
            {'key': 'customer', 'name': 'حساب العميل', 'journal_id': False,                                      'existing_record': False},
        ]

        methods_to_link = []

        for method_def in required_methods:
            existing = method_def['existing_record']
            
            if not existing:
                if method_def['key'] == 'bank' and method_def['journal_id']:
                    existing = self.env['pos.payment.method'].search([
                        ('journal_id', '=', method_def['journal_id']),
                        ('company_id', '=', company_id)
                    ], limit=1)
                elif method_def['key'] == 'customer':
                    existing = self.env['pos.payment.method'].search([
                        ('name', '=', method_def['name']),
                        ('company_id', '=', company_id),
                        ('journal_id', '=', False)
                    ], limit=1)

            # إذا لم تكن طريقة الدفع موجودة نهائياً، قم بإنشائها
            if not existing:
                create_vals = {'name': method_def['name'], 'company_id': company_id}
                if method_def['journal_id']:
                    create_vals['journal_id'] = method_def['journal_id']
                
                sp2 = 'sp_create_pm'
                self.env.cr.execute(f'SAVEPOINT "{sp2}"')
                try:
                    existing = self.env['pos.payment.method'].sudo().create(create_vals)
                    self.env.cr.execute(f'RELEASE SAVEPOINT "{sp2}"')
                except Exception as e:
                    self.env.cr.execute(f'ROLLBACK TO SAVEPOINT "{sp2}"')
                    _logger.error(f"Failed to create payment method '{method_def['name']}': {e}")
                    continue

            # إذا كانت طريقة الكاش الرئيسية تفتقد لربط اليومية، نقوم بإصلاحها فوراً
            if method_def['key'] == 'cash' and existing and not existing.journal_id and method_def['journal_id']:
                existing.sudo().write({'journal_id': method_def['journal_id']})

            if existing:
                methods_to_link.append(existing.id)

        # 5. التحديث الصارم والنهائي لطرق الدفع المسموحة للجهاز (3 طرق فقط لا غير)
        if methods_to_link:
            # نتأكد أن المصفوفة تحتوي على عناصر فريدة بدون تكرار بالخطأ
            methods_to_link = list(set(methods_to_link))
            
            sp3 = 'sp_link_pm'
            self.env.cr.execute(f'SAVEPOINT "{sp3}"')
            try:
                # مسح كامل للجدول الوسيط الخاص بالربط لهذا الجهاز أولاً لضمان تصفير الطرق القديمة
                self.env.cr.execute(
                    "DELETE FROM pos_config_pos_payment_method_rel WHERE pos_config_id = %s", 
                    (config_id_int,)
                )
                # إعادة ربط الطرق الـ 3 الصحيحة فقط
                pos_config.sudo().write({'payment_method_ids': [(6, 0, methods_to_link)]})
                self.env.cr.execute(f'RELEASE SAVEPOINT "{sp3}"')
            except Exception as e:
                self.env.cr.execute(f'ROLLBACK TO SAVEPOINT "{sp3}"')
                _logger.error(f"Failed to refresh payment methods on config: {e}")

        return pos_config.payment_method_ids.filtered(lambda pm: pm.is_cash_count)[:1]

    @api.model
    def get_pos_master_data_rpc(self, *args, **kwargs):
        """Loads foundational data needed to operate the terminal."""
        config_id   = kwargs.get('config_id')   or (args[0] if len(args) > 0 else None)
        page        = kwargs.get('page')        or (args[1] if len(args) > 1 else 1)
        limit       = kwargs.get('limit')       or (args[2] if len(args) > 2 else 50)
        category_id = kwargs.get('category_id') or (args[3] if len(args) > 3 else None)
        location_id = kwargs.get('location_id') or (args[4] if len(args) > 4 else None)
        search_text = kwargs.get('search_text') or (args[5] if len(args) > 5 else '')

        if not config_id:
            return {'status': 'error', 'message': 'Missing config_id parameter'}
        try:
            config_id_int = int(config_id)
        except (ValueError, TypeError):
            return {'status': 'error', 'message': f'Invalid config_id: {config_id}'}

        pos_config = self.browse(config_id_int)
        if not pos_config.exists():
            return {'status': 'error', 'message': 'POS Config not found'}

        config_company_id = pos_config.company_id.id

        page  = max(1, int(page))
        limit = max(1, int(limit))
        offset = (page - 1) * limit

        warehouses = self.env['stock.warehouse'].search_read(
            ['|', ('company_id', '=', False), ('company_id', '=', config_company_id)],
            ['id', 'name', 'code', 'lot_stock_id']
        )

        has_location_filter = False
        if location_id and str(location_id).strip() not in ('', 'null'):
            try:
                location_ids = [int(location_id)]
                has_location_filter = True
            except (ValueError, TypeError):
                location_ids = [w['lot_stock_id'][0] for w in warehouses if w['lot_stock_id']]
        else:
            location_ids = [w['lot_stock_id'][0] for w in warehouses if w['lot_stock_id']]

        product_domain = [
            ('available_in_pos', '=', True),
            '|', ('company_id', '=', False), ('company_id', '=', config_company_id),
        ]

        if category_id and str(category_id).strip() not in ('', 'null'):
            try:
                product_domain.append(('pos_categ_ids', 'in', [int(category_id)]))
            except (ValueError, TypeError):
                pass

        available_product_ids = []
        if has_location_filter:
            quant_domain = [('location_id', '=', location_ids[0])]
            if not pos_config.allow_out_of_stock_sale:
                quant_domain.append(('quantity', '>', 0.0))
            quant_records = self.env['stock.quant'].search(quant_domain)
            available_product_ids = list(set(quant_records.mapped('product_id.id')))
            product_domain.append(('id', 'in', available_product_ids))

        if search_text:
            search_text = search_text.strip()
            product_domain.append('|')
            product_domain.append('|')
            product_domain.append(('name', 'ilike', search_text))
            product_domain.append(('default_code', 'ilike', search_text))
            product_domain.append(('barcode', 'ilike', search_text))

        category_counts_domain = [('available_in_pos', '=', True)]
        if has_location_filter:
            category_counts_domain.append(('id', 'in', available_product_ids))
        if search_text:
            category_counts_domain.append('|')
            category_counts_domain.append('|')
            category_counts_domain.append(('name', 'ilike', search_text))
            category_counts_domain.append(('default_code', 'ilike', search_text))
            category_counts_domain.append(('barcode', 'ilike', search_text))

        count_data = self.env['product.product'].read_group(
            domain=category_counts_domain,
            fields=['pos_categ_ids'],
            groupby=['pos_categ_ids']
        )
        category_counts_map = {}
        for line in count_data:
            if line.get('pos_categ_ids'):
                cat_id = line['pos_categ_ids'][0]
                category_counts_map[cat_id] = line['pos_categ_ids_count']

        raw_categories = self.env['pos.category'].search_read(
            ['|', ('company_id', '=', False), ('company_id', '=', config_company_id)],
            ['id', 'name', 'parent_id']
        )
        category_map   = {c['id']: c['name'] for c in raw_categories}

        categories_with_counts = [{
            'id':            cat['id'],
            'name':          cat['name'],
            'parent_id':     cat['parent_id'],
            'product_count': category_counts_map.get(cat['id'], 0)
        } for cat in raw_categories]

        payment_methods = self.env['pos.payment.method'].search_read(
            [('id', 'in', pos_config.payment_method_ids.ids)],
            ['id', 'name', 'is_cash_count']
        )
        pricelists = self.env['product.pricelist'].search_read(
            ['|', ('company_id', '=', False), ('company_id', '=', config_company_id)],
            ['id', 'name', 'currency_id']
        )

        total_products = self.env['product.product'].search_count(product_domain)
        products = self.env['product.product'].search(product_domain, limit=limit, offset=offset, order='id asc')

        # Pre-compute attribute lines per template (all attribute options for variant picker)
        template_ids = set(prod.product_tmpl_id.id for prod in products)
        template_attr_map = {}
        for tmpl_id in template_ids:
            tmpl = self.env['product.template'].browse(tmpl_id)
            lines = []
            for line in tmpl.attribute_line_ids:
                lines.append({
                    'id':     line.attribute_id.id,
                    'name':   line.attribute_id.name,
                    'values': [{
                        'id':          ptav.product_attribute_value_id.id,
                        'name':        ptav.product_attribute_value_id.name,
                        'price_extra': ptav.price_extra,
                    } for ptav in line.product_template_value_ids],
                })
            template_attr_map[tmpl_id] = lines

        product_list = []
        for prod in products:
            stock_per_location = {}
            for loc_id in location_ids:
                qty = prod.with_context(location=loc_id).qty_available
                stock_per_location[str(loc_id)] = qty

            # Collect attribute values for this variant
            attribute_values = []
            for ptav in prod.product_template_attribute_value_ids:
                attribute_values.append({
                    'attr_id':    ptav.attribute_id.id,
                    'attr_name':  ptav.attribute_id.name,
                    'value_id':   ptav.id,
                    'value_name': ptav.name,
                })

            image_data = prod.image_1920
            image_base64 = image_data if isinstance(image_data, str) else (image_data.decode('utf-8') if image_data else None)

            product_list.append({
                'id':               prod.id,
                'product_tmpl_id':  prod.product_tmpl_id.id,
                'display_name':     prod.display_name,
                'lst_price':        prod.lst_price,
                'barcode':          prod.barcode,
                'to_weight':        bool(prod.to_weight),
                'type':             prod.type or 'product',
                'weight':           prod.weight or 0.0,
                'stock_by_location': stock_per_location,
                'pos_categories':   [{'id': cid, 'name': category_map.get(cid, '')} for cid in prod.pos_categ_ids.ids],
                'taxes_id':         prod.taxes_id.ids,
                'attribute_values': attribute_values,
                'attribute_lines':  template_attr_map.get(prod.product_tmpl_id.id, []),
                'price_extra':      sum(ptav.price_extra for ptav in prod.product_template_attribute_value_ids),
                'image_1920':       image_base64,
            })

        total_pages = (total_products + limit - 1) // limit

        return {
            'status': 'success',
            'data': {
                'warehouses':           warehouses,
                'products':             product_list,
                'categories':           categories_with_counts,
                'pricelists':           pricelists,
                'payment_methods':      payment_methods,
                'default_pricelist_id': pos_config.pricelist_id.id,
                'allow_out_of_stock_sale': pos_config.allow_out_of_stock_sale,
                'pagination': {
                    'total_items':  total_products,
                    'total_pages':  total_pages,
                    'current_page': page,
                    'limit':        limit,
                    'has_next':     page < total_pages,
                }
            }
        }

    @api.model
    def create_new_register_rpc(self, name):
        """Creates a new POS Terminal configuration."""
        if not name or not name.strip():
            return {'status': 'error', 'message': 'اسم الجهاز مطلوب'}

        name = name.strip()
        self.env.cr.execute('SAVEPOINT sp_create_register')
        try:
            defaults = self.default_get(['name', 'pricelist_id', 'warehouse_id'])
            values   = {'name': name}

            if defaults.get('pricelist_id'):
                values['pricelist_id'] = defaults['pricelist_id']
            else:
                pricelist = self.env['product.pricelist'].search(
                    self.env['product.pricelist']._check_company_domain(self.env.company), limit=1,
                )
                if not pricelist:
                    pricelist = self.env['product.pricelist'].create({
                        'name':        'قائمة أسعار افتراضية تلقائية',
                        'currency_id': self.env.company.currency_id.id,
                        'company_id':  self.env.company.id,
                    })
                values['pricelist_id'] = pricelist.id

            if defaults.get('warehouse_id'):
                values['warehouse_id'] = defaults['warehouse_id']
            else:
                warehouse = self.env['stock.warehouse'].search(
                    self.env['stock.warehouse']._check_company_domain(self.env.company), limit=1,
                )
                if not warehouse:
                    self.env.cr.execute('ROLLBACK TO SAVEPOINT sp_create_register')
                    return {'status': 'error', 'message': 'لا يوجد مخزن. الرجاء إنشاء واحد أولاً.'}
                values['warehouse_id'] = warehouse.id

            new_config = self.create(values)
            self.env.cr.execute('RELEASE SAVEPOINT sp_create_register')
            return {
                'status':  'success',
                'id':      new_config.id,
                'message': f'تم إنشاء جهاز "{name}" بنجاح'
            }
        except Exception as e:
            self.env.cr.execute('ROLLBACK TO SAVEPOINT sp_create_register')
            return {'status': 'error', 'message': str(e)}

    @api.model
    def get_all_registers_with_status_rpc(self, *args, **kwargs):
        """Fetches all POS terminals with their most recent session state."""
        try:
            configs = self.sudo().search_read(
                self._get_company_domain(), ['id', 'name'],
            )
            if not configs:
                return {'status': 'success', 'data': []}

            config_ids = [c['id'] for c in configs]
            sessions   = self.env['pos.session'].sudo().search_read(
                [('config_id', 'in', config_ids)],
                ['id', 'config_id', 'state'],
                order='id desc'
            )

            config_id_to_session = {}
            for s in sessions:
                raw_cid  = s['config_id'][0] if isinstance(s['config_id'], (list, tuple)) else s['config_id']
                cid_str  = str(raw_cid)
                raw_state = s.get('state') or 'closed'
                if cid_str not in config_id_to_session:
                    config_id_to_session[cid_str] = {
                        'session_id': s['id'] or 0,
                        'state':      str(raw_state),
                    }

            state_map = {
                'opening_control': 'opening_control',
                'draft':           'opening_control',
                'opened':          'opened',
                'closing_control': 'closing_control',
                'closed':          'closed',
            }

            registers_list = []
            for c in configs:
                session_info  = config_id_to_session.get(str(c['id']))
                mapped_state  = 'closed'
                session_id_val = False

                if session_info:
                    session_id_val = session_info['session_id'] or False
                    mapped_state   = state_map.get(session_info['state'], 'unknown')

                registers_list.append({
                    'id':            c['id'],
                    'name':          str(c['name'] or ''),
                    'session_id':    session_id_val,
                    'session_state': mapped_state,
                })

            return {'status': 'success', 'data': registers_list}
        except Exception as e:
            return {'status': 'error', 'message': f'Python list execution failed: {str(e)}'}


# ─────────────────────────────────────────────────────────────────────────────
# 2. PosSession
# ─────────────────────────────────────────────────────────────────────────────
class PosSession(models.Model):
    _inherit = 'pos.session'

    @api.model
    def get_session_summary_rpc(self, *args, **kwargs):
        session_id = kwargs.get('session_id') or (args[0] if len(args) > 0 else None)
        if isinstance(session_id, list) and len(session_id) > 0:
            session_id = session_id[0]
        if not session_id:
            return {'status': 'error', 'message': 'Missing session ID parameter.'}

        try:
            session = self.browse(int(session_id))
            if not session.exists():
                return {'status': 'error', 'message': f'Session {session_id} not found.'}

            return {
                'status':     'success',
                'session_id': session.id,
                'name':       session.name,
                'state':      session.state,
                'start_at':   fields.Datetime.to_string(session.start_at) if session.start_at else False,
                'stop_at':    fields.Datetime.to_string(session.stop_at)  if session.stop_at  else False,
                'user': {
                    'id':   session.user_id.id,
                    'name': session.user_id.name,
                } if session.user_id else False,
                'config': {
                    'id':   session.config_id.id,
                    'name': session.config_id.name,
                } if session.config_id else False,
                'financials': {
                    'total_orders_count':               session.order_count,
                    'total_payments_amount':            float(sum(
                        o.amount_paid for o in session.order_ids if o.state in ('paid', 'done', 'invoiced')
                    )),
                    'cash_register_balance_start':      float(session.cash_register_balance_start      or 0.0),
                    'cash_register_balance_end':        float(session.cash_register_balance_end        or 0.0),
                    'cash_register_balance_end_real':   float(session.cash_register_balance_end_real   or 0.0),
                }
            }
        except Exception as e:
            return {'status': 'error', 'message': f'Python Exception: {str(e)}'}

    #  ملاحظة: تم حذف دالة create_pos_order_rpc القديمة بالكامل من هنا لمنع مشاكل الكاش المحاسبية.

    @api.model
    def control_pos_session_rpc(self, *args, **kwargs):
        """Handles session open/close/status lifecycle."""
        config_id    = kwargs.get('config_id')    or (args[0] if len(args) > 0 else None)
        action       = kwargs.get('action')       or (args[1] if len(args) > 1 else None)
        opening_cash = kwargs.get('opening_cash') or (args[2] if len(args) > 2 else 0.0)
        force_close  = kwargs.get('force_close') in (True, 'true', 'True', 1, '1') or (args[3] if len(args) > 3 else False)

        if not config_id or str(config_id).strip() in ('', 'config_id'):
            return {'status': 'error', 'message': 'Missing or invalid config_id'}
        try:
            config_id_int = int(config_id)
        except (ValueError, TypeError):
            return {'status': 'error', 'message': f'Invalid config_id: {config_id}'}

        current_session = self.search([
            ('config_id', '=', config_id_int),
            ('state', 'in', ['opened', 'closing_control'])
        ], order='id desc', limit=1)

        if action == 'status':
            return {
                'status': 'success',
                'session': {
                    'session_id':    current_session.id,
                    'session_state': current_session.state,
                    'session_active': True,
                } if current_session else False,
            }
        current_session = self.search([
            ('config_id', '=', config_id_int),
            ('state', 'in', ['opened', 'opening_control', 'closing_control']) # أضفنا كل الحالات النشطة هنا
        ], order='id desc', limit=1)
        
        if action == 'open':
            self.env['pos.config']._ensure_payment_methods_before_open(config_id_int)
            
            # إذا وُجدت أي جلسة نشطة بالفعل، أرجعها فوراً للـ Frontend ولا تقم بإنشاء جلسة جديدة
            if current_session:
                # إذا كانت في مرحلة الرقابة الافتتاحية، قم بتأكيد فتحها تلقائياً بالرصيد الممرر
                if current_session.state == 'opening_control':
                    try:
                        current_session.set_opening_control(float(opening_cash), "Opening balance via RPC")
                    except Exception:
                        pass
                        
                return {
                    'status':  'success',
                    'message': 'هناك جلسة نشطة بالفعل لهذه النقطة تم ربطك بها',
                    'session': {
                        'session_id':    current_session.id,
                        'session_state': current_session.state,
                        'session_active': current_session.state in ['opened', 'opening_control'],
                    },
                }

            # إذا لم توجد أي جلسة نهائياً، هنا فقط نقوم بإنشاء واحدة جديدة بأمان
            new_session = self.create({'config_id': config_id_int, 'user_id': self.env.uid})
            new_session.action_pos_session_open()
            try:
                new_session.set_opening_control(float(opening_cash), "Opening balance via RPC")
            except Exception:
                pass

            return {
                'status':  'success',
                'message': 'Session initialized successfully',
                'session': {
                    'session_id':    new_session.id,
                    'session_state': new_session.state,
                    'session_active': new_session.state == 'opened',
                },
            }

        if action == 'close':
            if not current_session:
                return {'status': 'error', 'message': 'No active session found to close.'}

            session_id   = current_session.id
            session_name = current_session.name

            draft_orders = current_session.order_ids.filtered(lambda o: o.state == 'draft')
            if draft_orders:
                if force_close:
                    draft_orders.sudo().write({'state': 'cancel'})
                else:
                    return {
                        'status': 'error',
                        'message': f'Cannot close: {len(draft_orders)} draft order(s) still open.',
                        'draft_count': len(draft_orders),
                    }

            try:
                # تهيئة بيئة تشغيلية مطابقة للشركة والسياق المحاسبي لمنع الأخطاء المتداخلة
                session_sudo = current_session.sudo().with_company(current_session.company_id)
                
                # تأمين وجود سجل الجلسة حياً داخل الـ Cache قبل إطلاق الفلاش والمحاسبة
                session_sudo.read(['config_id', 'state', 'company_id', 'name', 'cash_register_balance_end_real'])

                if current_session.config_id.cash_control:
                    real_closing = float(opening_cash) if opening_cash else 0.0
                    session_sudo.write({'cash_register_balance_end_real': real_closing})

                if session_sudo.state == 'opened':
                    session_sudo.write({
                        'state':   'closing_control',
                        'stop_at': fields.Datetime.now(),
                    })
                    # نقوم بعمل flush للنظام لترحيل الحالة أولاً بأمان وبدون حذف الكاش
                    self.env.cr.flush()

                if session_sudo.state == 'closing_control':
                    try:
                        # استدعاء المعالج الأصلي المعتمد للإغلاق والترحيل المحاسبي
                        result = session_sudo._validate_session()
                        # _validate_session قد ترجع قاموس (نافذة الإغلاق القسري) عندما يكون القيد محاسبي غير متوازن
                        # وفي هذه الحالة تقوم بعمل rollback للترانزاكشن مما يعيد حالة الجلسة إلى opened
                        # نحتاج للتعامل مع هذه الحالة يدوياً
                        if isinstance(result, dict):
                            raise UserError(result.get('name', 'Account move unbalanced'))
                    except Exception as val_err:
                        _logger.error(f"_validate_session raised: {val_err}", exc_info=True)
                        # حل بديل طارئ في حال تعطل قيود التسوية التلقائية لإجبار الجلسة على الإغلاق
                        self.env['pos.order'].search([
                            ('session_id', '=', session_id),
                            ('state', '=', 'paid')
                        ]).sudo().write({'state': 'done'})
                        session_sudo.write({'state': 'closed'})
                        self.env.cr.flush()

                # جلب الحالة النهائية المستقرة في قاعدة البيانات بعد الـ Validation
                # إعادة تحميل cache الجلسة لأن rollback قد يكون مسح التغييرات
                self.env['pos.session'].sudo().browse(session_id).read(['state'])
                final_state = self.env['pos.session'].sudo().browse(session_id).state

                return {
                    'status':  'success',
                    'message': f'Session {session_name} closed successfully.',
                    'session': {
                        'session_id':    session_id,
                        'session_state': final_state,
                        'session_active': final_state in ['opened', 'closing_control'],
                    },
                }
            except Exception as e:
                _logger.error(f"POS Close outer exception: {str(e)}", exc_info=True)
                return {'status': 'error', 'message': f'Closing failed: {str(e)}'}

        return {'status': 'error', 'message': f'Invalid action: {action}'}
    @api.model
    def control_cash_movement_rpc(self, *args, **kwargs):
        session_id = kwargs.get('session_id') or (args[0] if len(args) > 0 else None)
        amount     = kwargs.get('amount') if kwargs.get('amount') is not None else (args[1] if len(args) > 1 else None)
        reason     = kwargs.get('reason') or (args[2] if len(args) > 2 else '')

        if not session_id or amount is None:
            return {'status': 'error', 'message': 'Missing parameters'}

        session = self.browse(int(session_id))
        if not session.exists() or session.state != 'opened':
            return {'status': 'error', 'message': 'Session is not active'}

        try:
            raw_amount = float(amount)
            if raw_amount == 0.0:
                return {'status': 'error', 'message': 'المبلغ لا يمكن أن يكون صفراً'}

            cash_method = session.payment_method_ids.filtered(lambda pm: pm.is_cash_count)[:1]
            if not cash_method:
                cash_method = session.config_id._ensure_payment_methods_before_open(session.config_id.id)

            if not cash_method:
                return {'status': 'error', 'message': 'Could not find or create a cash payment method.'}

            type_token          = 'in' if raw_amount > 0 else 'out'
            val_amount          = abs(raw_amount)
            reason_str          = reason or ('Cash In' if raw_amount > 0 else 'Cash Out')
            translated_type_str = 'Cash In' if raw_amount > 0 else 'Cash Out'

            session.try_cash_in_out(
                type_token,
                val_amount,
                reason_str,
                extras={
                    'translatedType':  translated_type_str,
                    'formattedAmount': f"{val_amount:.2f}"
                }
            )

            session.invalidate_recordset(['cash_register_balance_end'])
            return {
                'status':             'success',
                'message':            'تم تسجيل حركة النقدية بنجاح',
                'current_total_cash': session.cash_register_balance_end,
            }
        except Exception as e:
            return {'status': 'error', 'message': f'Python Exception: {str(e)}'}


# ─────────────────────────────────────────────────────────────────────────────
# 3. PosOrder
# ─────────────────────────────────────────────────────────────────────────────
class PosOrder(models.Model):
    _inherit = 'pos.order'

    @api.model
    def receive_pos_order_rpc(self, *args, **kwargs):
        location_id = kwargs.get('location_id') or (args[0] if len(args) > 0 else None)
        order_data  = kwargs.get('order_data')  or (args[1] if len(args) > 1 else None)

        if not order_data or not location_id:
            return {'status': 'error', 'message': 'Missing order payload or location'}

        try:
            res = self.create_pos_order_rpc(
                session_id=order_data.get('pos_session_id'), 
                payload=order_data, 
                target_location_id=location_id
            )
            return res
        except Exception as e:
            _logger.error("RPC POS Direct Order Placement Error: %s", str(e), exc_info=True)
            return {'status': 'error', 'message': str(e)}

    @api.model
    def create_pos_order_rpc(self, *args, **kwargs):
        """
        إنشاء طلب نقطة بيع بالاعتماد على ميثود السجل القياسي العادي (create).
        يدعم ربط مصفوفة الضرائب tax_ids القادمة من الـ Frontend مباشرة ببند الطلب ماليّاً.
        """
        session_id = kwargs.get('session_id') or (args[0] if len(args) > 0 else None)
        payload    = kwargs.get('payload')    or (args[1] if len(args) > 1 else {})
        forced_loc = kwargs.get('target_location_id') or payload.get('target_location_id', False)

        if not session_id or not payload:
            return {
                'status': 'error',
                'message': 'Mandatory parameters missing: session_id or payload.'
            }

        try:
            # 1. التحقق من الجلسة وضبط المتغيرات البيئية للشركة
            session = self.env['pos.session'].browse(int(session_id))
            if not session.exists() or session.state != 'opened':
                return {'status': 'error', 'message': f'POS Session {session_id} is missing or not open.'}

            items = payload.get('items', payload.get('lines', []))
            payments = payload.get('payments', payload.get('statement_ids', []))
            customer_id = payload.get('customer_id', payload.get('partner_id', False))

            if not items:
                return {'status': 'error', 'message': 'No items in order payload.'}

            # 2. بناء بنود الطلب يدوياً وحساب الأسعار والضرائب
            product_ids = [int(i.get('product_id')) for i in items if i.get('product_id')]
            products_map = {p.id: p for p in self.env['product.product'].browse(product_ids)}

            # Auto-detect location with stock if none selected
            if not forced_loc and product_ids:
                StockQuant = self.env['stock.quant']
                domain = [
                    ('product_id', 'in', product_ids),
                    ('location_id.usage', '=', 'internal'),
                    ('quantity', '>', 0),
                ]
                quants = StockQuant.search(domain)
                if quants:
                    location_scores = {}
                    for q in quants:
                        location_scores[q.location_id.id] = location_scores.get(q.location_id.id, 0) + q.quantity
                    if location_scores:
                        forced_loc = max(location_scores, key=location_scores.get)

            order_lines = []
            total_order_subtotal = 0.0

            for item in items:
                prod_id = int(item.get('product_id'))
                qty = float(item.get('quantity', item.get('qty', 1)))
                price_unit = float(item.get('price', item.get('price_unit', 0)))
                discount = float(item.get('discount', 0))
                product = products_map.get(prod_id)

                if not product:
                    continue

                subtotal = qty * price_unit * (1.0 - discount / 100.0)
                total_order_subtotal += subtotal

                # استخراج معرفات الضرائب المرسلة من الواجهة الأمامية
                frontend_tax_ids = item.get('tax_ids', [])
                
                line_vals = {
                    'product_id': prod_id,
                    'qty': qty,
                    'price_unit': price_unit,
                    'discount': discount,
                    'price_subtotal': subtotal,
                    'price_subtotal_incl': subtotal,
                }

                # 💡 ربط مصفوفة الضرائب بالبند إذا تم إرسالها من الـ Frontend
                if frontend_tax_ids:
                    line_vals['tax_ids'] = [(6, 0, [int(tid) for tid in frontend_tax_ids])]
                    # حساب السعر شامل الضريبة إذا كانت هناك ضرائب
                    taxes = self.env['account.tax'].browse([int(tid) for tid in frontend_tax_ids])
                    if taxes:
                        tax_results = taxes.compute_all(price_unit * (1.0 - discount / 100.0), quantity=qty)
                        line_vals['price_subtotal_incl'] = tax_results['total_included']

                order_lines.append((0, 0, line_vals))

            # معالجة رسوم الخدمة الإضافية (إن وجدت)
            service_fee = float(payload.get('service_fee', 0))
            if payload.get('service_fee_type') == 'percent' and total_order_subtotal > 0:
                service_fee = (service_fee / 100.0) * total_order_subtotal

            if service_fee > 0:
                service_product = self.env['product.product'].search([
                    ('type', '=', 'service'),
                    ('available_in_pos', '=', True)
                ], limit=1)
                if service_product:
                    order_lines.append((0, 0, {
                        'product_id': service_product.id,
                        'qty': 1,
                        'price_unit': service_fee,
                        'discount': 0,
                        'price_subtotal': service_fee,
                        'price_subtotal_incl': service_fee,
                    }))

            # 3. إعداد مصفوفة طرق الدفع للطلب وحساب إجمالي المدفوعات
            order_payments = []
            payments_total = 0.0
            for pmt in payments:
                pay_method_id = pmt.get('method_id') or pmt.get('payment_method_id')
                amount = float(pmt.get('amount', 0.0))
                if pay_method_id and amount > 0:
                    payments_total += amount
                    order_payments.append((0, 0, {
                        'payment_method_id': int(pay_method_id),
                        'amount': amount,
                        'payment_date': fields.Datetime.now(),
                    }))

            # Apply order-level discount
            order_discount = float(payload.get('order_discount', 0))
            order_discount_type = payload.get('order_discount_type', 'amount')
            
            if order_discount_type == 'percent' and total_order_subtotal > 0:
                order_discount = (order_discount / 100.0) * total_order_subtotal
            
            final_total = total_order_subtotal + (service_fee if service_fee > 0 else 0) - order_discount
            if final_total < 0:
                final_total = 0.0
            # taxes computed server-side by _compute_prices() during action_pos_order_paid()

            # 4. تجميع بيانات الطلب الرئيسي مع تلبية شروط قواعد البيانات الصارمة للأعمدة المطلوبة
            pos_order_vals = {
                'name': payload.get('name', f"RPC-{uuid.uuid4().hex[:6].upper()}"),
                'session_id': session.id,
                'company_id': session.company_id.id,
                'partner_id': int(customer_id) if customer_id else False,
                'lines': order_lines,
                'payment_ids': order_payments,
                'amount_total': final_total,
                'amount_tax': float(payload.get('amount_tax', 0)),
                'amount_paid': payments_total,
                'amount_return': max(0.0, payments_total - final_total),
                'date_order': fields.Datetime.now(),
            }

            # فحص ديناميكي آمن لحقل الملاحظات لتفادي الـ Invalid field error باختلاف الإصدارات
            raw_note = payload.get('note', payload.get('general_note', ''))
            if raw_note:
                if 'note' in self._fields:
                    pos_order_vals['note'] = raw_note
                elif 'general_note' in self._fields:
                    pos_order_vals['general_note'] = raw_note
                elif 'pos_note' in self._fields:
                    pos_order_vals['pos_note'] = raw_note

            # 5. إنشاء السجل الفعلي في قاعدة البيانات
            # تمرير forced_loc عبر context للـ create method لاستخدامه في picking
            pos_model = self.sudo().with_company(session.company_id)
            if forced_loc:
                pos_model = pos_model.with_context(force_picking_location=forced_loc)

            # إنشاء السجل الفعلي في قاعدة البيانات
            new_order = pos_model.create(pos_order_vals)

            # 6. تحديث حسابات الطلب وتأكيد عمليات المخازن والترحيل تلقائياً
            new_order._compute_total_all_at_once() if hasattr(new_order, '_compute_total_all_at_once') else None

            if new_order.payment_ids:
                if new_order.amount_return > 0:
                    cash_method = session.payment_method_ids.filtered('is_cash_count')[:1]
                    if cash_method:
                        new_order.add_payment({
                            'name': 'return',
                            'pos_order_id': new_order.id,
                            'amount': -new_order.amount_return,
                            'payment_date': fields.Datetime.now(),
                            'payment_method_id': cash_method.id,
                            'is_change': True,
                        })
                new_order.action_pos_order_paid()
            
            if hasattr(new_order, '_create_order_picking'):
                new_order._create_order_picking()
            elif hasattr(new_order, 'create_picking'):
                new_order.create_picking()

            return {
                'status':     'success',
                'order_id':   new_order.id,
                'order_name': new_order.pos_reference or new_order.name,
                'state':      new_order.state,
                'message':    'تم إنشاء وتأكيد الطلب وترحيله مع الحفاظ التام على مصفوفة الضرائب الممررة.'
            }

        except Exception as e:
            _logger.error("Standard POS Order Fallback Mechanism Crash: %s", str(e), exc_info=True)
            return {
                'status': 'error',
                'message': f'Alternative processing failure: {str(e)}'
            }
# ─────────────────────────────────────────────────────────────────────────────
# 4. ProductProduct
# ─────────────────────────────────────────────────────────────────────────────
class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model
    def check_live_stock_rpc(self, *args, **kwargs):
        try:
            product_id = kwargs.get('product_id') or (args[0] if len(args) > 0 else None)
            if not product_id:
                return {'status': 'error', 'message': 'Missing product_id parameter'}

            product = self.browse(int(product_id))
            if not product.exists():
                return {'status': 'error', 'message': 'Product not found'}

            warehouses = self.env['stock.warehouse'].search(self._get_company_domain())
            stock_balances = {}
            for wh in warehouses:
                if wh.lot_stock_id:
                    qty = product.with_context(location=wh.lot_stock_id.id).qty_available
                    # تأمين القيمة: إذا كانت الكمية None، نجعلها 0.0 فوراً
                    safe_qty = float(qty) if qty is not None else 0.0
                    
                    stock_balances[str(wh.id)] = {
                        'warehouse_name': wh.name or '', # تأمين السلسلة النصية
                        'warehouse_code': wh.code or '',
                        'location_id':    wh.lot_stock_id.id,
                        'qty_available':  safe_qty,
                    }

            return {
                'status':             'success',
                'product_id':         product.id,
                'stock_by_warehouse': stock_balances,
            }
            
        except Exception as e:
            # تأمين أخير: منع الدالة من رمي None في حال حدوث أي خطأ غير متوقع
            return {
                'status': 'error',
                'message': f'Unexpected backend error: {str(e)}'
            }