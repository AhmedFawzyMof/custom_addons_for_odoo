import math
from odoo import models, fields, api

class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model
    def get_pos_frontend_customers(self, params=None):
        """
        UPDATED: Fetches retail customers and external business companies.
        Strictly excludes internal employees, employee parent companies, 
        and the user's CURRENT main company entity.
        """
        if params is None:
            params = {}

        try:
            page = max(1, int(params.get('page', 1)))
        except (ValueError, TypeError):
            page = 1
            
        limit = 20
        offset = (page - 1) * limit
        search_query = params.get('search', '').strip()
        type_filter = params.get('type', 'الكل')

        # 1. Base Security Filtering (Exclude Users, Employees & Main Company)
        active_users = self.env['res.users'].search([])
        exclude_partner_ids = active_users.mapped('partner_id.id')
        
        # Find parent companies of internal employees/users
        parent_partners = active_users.mapped('partner_id.parent_id.id')
        
        # CRITICAL FIX: Find your main company's partner ID and inject it into the exclusion array
        current_main_company_partner = self.env.company.partner_id.id
        
        # Merge all exclusions dynamically together into a unique list
        exclude_partner_ids = list(set(exclude_partner_ids + parent_partners + [current_main_company_partner]))

        domain = [
            ('id', 'not in', exclude_partner_ids),
            ('active', '=', True)
        ]

        # 2. Filter by Segment (Retail Person vs Corporate B2B)
        if type_filter == 'فرد':
            domain.append(('is_company', '=', False))
        elif type_filter == 'B2B':
            domain.append(('is_company', '=', True))

        # 3. Text Search Matching
        if search_query:
            domain.extend([
                '|', '|',
                ('name', 'ilike', search_query),
                ('phone', 'ilike', search_query),
                ('email', 'ilike', search_query)
            ])

        # 4. Pagination Calculations
        total_count = self.search_count(domain)
        total_pages = math.ceil(total_count / limit) if total_count > 0 else 1

        partners = self.search(domain, offset=offset, limit=limit, order='write_date desc')
        partner_ids = partners.ids

        if not partner_ids:
            return {
                'success': True, 'totalItems': 0, 'totalPages': 1, 'currentPage': page,
                'itemsPerPage': limit, 'data': [], 'meta': {'totalB2B': 0, 'activeRecent': 0, 'loyaltyCount': 0}
            }

        # ── Data Map A: Loyalty Cards ──
        loyalty_map = {}
        loyalty_cards = self.env['loyalty.card'].search([('partner_id', 'in', partner_ids)])
        for card in loyalty_cards:
            pid = card.partner_id.id
            prog_name = card.program_id.name or ''
            if pid not in loyalty_map:
                loyalty_map[pid] = {'points': 0, 'programName': prog_name}
            loyalty_map[pid]['points'] += card.points or 0

        # ── Data Map B: Total Spent ──
        total_spent_map = {}
        sale_orders = self.env['sale.order'].search([
            ('partner_id', 'in', partner_ids),
            ('state', 'not in', ['draft', 'cancel', 'sent'])
        ])
        for order in sale_orders:
            pid = order.partner_id.id
            total_spent_map[pid] = total_spent_map.get(pid, 0.0) + (order.amount_total or 0.0)

        missing_spent_ids = [pid for pid in partner_ids if pid not in total_spent_map]
        if missing_spent_ids:
            invoices = self.env['account.move'].search([
                ('partner_id', 'in', missing_spent_ids),
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted')
            ])
            for inv in invoices:
                pid = inv.partner_id.id
                total_spent_map[pid] = total_spent_map.get(pid, 0.0) + (inv.amount_total or 0.0)

        # ── Data Map C: Last Transaction ──
        last_tx_map = {}
        latest_orders = self.env['sale.order'].search([
            ('partner_id', 'in', partner_ids),
            ('state', 'not in', ['draft', 'cancel', 'sent'])
        ], order='date_order asc')
        for order in latest_orders:
            last_tx_map[order.partner_id.id] = {
                'amount': order.amount_total,
                'date': str(order.date_order)[:10]
            }

        missing_tx_ids = [pid for pid in partner_ids if pid not in last_tx_map]
        if missing_tx_ids:
            latest_invoices = self.env['account.move'].search([
                ('partner_id', 'in', missing_tx_ids),
                ('move_type', 'in', ['out_invoice', 'out_refund']),
                ('state', '=', 'posted')
            ], order='invoice_date asc')
            for inv in latest_invoices:
                last_tx_map[inv.partner_id.id] = {
                    'amount': inv.amount_total,
                    'date': str(inv.invoice_date)
                }

        # 5. Build Payload JSON Map
        complete_customers = []
        for p in partners:
            pid = p.id
            tx = last_tx_map.get(pid, {'amount': 0, 'date': ''})
            loyalty = loyalty_map.get(pid, {'points': 0, 'programName': ''})
            total_spent = total_spent_map.get(pid, 0.0)
            
            points = loyalty['points'] if pid in loyalty_map else math.floor(total_spent / 10)

            tier = "فضي"
            prog_name = loyalty['programName'].lower() if loyalty['programName'] else ""
            if "بلاتيني" in prog_name or "platinum" in prog_name or total_spent >= 100000:
                tier = "بلاتيني"
            elif "ذهبي" in prog_name or "gold" in prog_name or total_spent >= 20000:
                tier = "ذهبي"

            complete_customers.append({
                'id': pid,
                'name': p.name or '',
                'email': p.email or '',
                'phone': p.mobile or p.phone or '',
                'type': 'B2B' if p.is_company else 'فرد',
                'tier': tier,
                'points': points,
                'address': ", ".join([f for f in [p.street, p.city] if f]) or '',
                'taxId': p.vat or 'N/A',
                'birthDate': str(p.create_date)[:10] if p.create_date else '',
                'lastTxAmount': tx['amount'],
                'lastTxTime': tx['date']
            })

        total_b2b = sum(1 for c in complete_customers if c['type'] == 'B2B')
        active_recent = sum(1 for c in complete_customers if c['points'] > 1000)
        loyalty_count = sum(1 for c in complete_customers if c['tier'] != 'فضي')

        return {
            'success': True,
            'totalItems': total_count,
            'totalPages': total_pages,
            'currentPage': page,
            'itemsPerPage': limit,
            'data': complete_customers,
            'meta': {
                'totalB2B': total_b2b,
                'activeRecent': active_recent,
                'loyaltyCount': loyalty_count
            }
        }

    @api.model
    def get_customer_detailed_ledger(self, params=None):
        """
        NEW DEDICATED FUNCTION: 
        Accepts a specific 'customer_id' and returns deep historical metrics, 
        address details, and a ledger stream of their last 10 transactions.
        """
        if params is None:
            params = {}

        customer_id = params.get('customer_id')
        if not customer_id:
            return {'success': False, 'message': 'Missing customer_id parameter'}

        # Fetch the specific partner record
        partner = self.browse(int(customer_id))
        if not partner.exists():
            return {'success': False, 'message': 'Customer record not found'}

        # Calculate Customer Total Lifetime Value (Sales Orders)
        total_spent = 0.0
        sale_orders = self.env['sale.order'].search([
            ('partner_id', '=', partner.id),
            ('state', 'not in', ['draft', 'cancel', 'sent'])
        ], order='date_order desc')
        
        for order in sale_orders:
            total_spent += (order.amount_total or 0.0)

        # Invoice backup total calculator
        if not sale_orders:
            invoices = self.env['account.move'].search([
                ('partner_id', '=', partner.id),
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted')
            ])
            for inv in invoices:
                total_spent += (inv.amount_total or 0.0)

        # Loyalty Point Extraction
        points = 0
        tier = "فضي"
        card = self.env['loyalty.card'].search([('partner_id', '=', partner.id)], limit=1)
        if card:
            points = card.points or 0
            prog_name = card.program_id.name.lower() if card.program_id.name else ""
            if "بلاتيني" in prog_name or "platinum" in prog_name or total_spent >= 100000:
                tier = "بلاتيني"
            elif "ذهبي" in prog_name or "gold" in prog_name or total_spent >= 20000:
                tier = "ذهبي"
        else:
            points = math.floor(total_spent / 10)
            if total_spent >= 100000:
                tier = "بلاتيني"
            elif total_spent >= 20000:
                tier = "ذهبي"

        history_ledger = []
        
        for o in sale_orders[:10]:
            status_lbl = 'مؤكد' if o.state == 'sale' else 'تم الشحن' if o.state == 'done' else o.state
            history_ledger.append({
                'reference': o.name,
                'date': str(o.date_order)[:10],
                'amount': o.amount_total,
                'type': 'طلب بيع',
                'status': o.state,
                'statusLabel': status_lbl
            })

        # 2. Add Posted Invoices and Refunds to the ledger stream
        client_invoices = self.env['account.move'].search([
            ('partner_id', '=', partner.id),
            ('move_type', 'in', ['out_invoice', 'out_refund']),
            ('state', '=', 'posted')
        ], order='invoice_date desc', limit=10)
        
        for inv in client_invoices:
            type_lbl = 'فاتورة عميل' if inv.move_type == 'out_invoice' else 'مرتجع فاتورة'
            history_ledger.append({
                'reference': inv.name,
                'date': str(inv.invoice_date),
                'amount': inv.amount_total if inv.move_type == 'out_invoice' else -inv.amount_total,
                'type': type_lbl,
                'status': 'posted',
                'statusLabel': 'مرحل'
            })

        # Sort the unified ledger cleanly by date descending and slice down to 10 records
        history_ledger = sorted(history_ledger, key=lambda x: x['date'], reverse=True)[:10]

        # Return full payload block
        return {
            'success': True,
            'data': {
                'id': partner.id,
                'name': partner.name or '',
                'email': partner.email or '',
                'phone': partner.mobile or partner.phone or '',
                'type': 'B2B' if partner.is_company else 'فرد',
                'tier': tier,
                'points': points,
                'companyName': partner.parent_id.name or '',
                'taxId': partner.vat or 'N/A',
                'birthDate': str(partner.create_date)[:10] if partner.create_date else '',
                'totalSpent': total_spent,
                
                # Detailed Address Object
                'addressDetails': {
                    'street': partner.street or '',
                    'street2': partner.street2 or '',
                    'city': partner.city or '',
                    'state': partner.state_id.name or '',
                    'zip': partner.zip or '',
                    'country': partner.country_id.name or '',
                    'fullAddress': ", ".join([f for f in [partner.street, partner.city, partner.state_id.name] if f]) or 'لا يوجد عنوان مسجل'
                },
                
                # Historic transactional arrays
                'transactions': history_ledger
            }
        }