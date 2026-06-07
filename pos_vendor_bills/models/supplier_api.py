from odoo import models, fields, api


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model
    def get_pos_suppliers(self, params=None):
        """
        Fetch suppliers for POS with pagination, search, and filters.
        Filters: supplier_rank > 0
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
        filter_status = params.get('status', 'all')

        domain = [
            ('supplier_rank', '>', 0),
            ('active', '=', True),
        ]

        if search_query:
            domain.extend([
                '|', '|', '|',
                ('name', 'ilike', search_query),
                ('phone', 'ilike', search_query),
                ('email', 'ilike', search_query),
                ('vat', 'ilike', search_query),
            ])

        total_count = self.search_count(domain)
        total_pages = (total_count + limit - 1) // limit if total_count > 0 else 1

        partners = self.search(domain, offset=offset, limit=limit, order='name asc')

        # Fetch purchase stats for each supplier
        supplier_ids = partners.ids
        purchase_stats = {}
        if supplier_ids:
            # Total purchased amount
            po_data = self.env['purchase.order'].read_group(
                domain=[
                    ('partner_id', 'in', supplier_ids),
                    ('state', 'in', ('purchase', 'done')),
                ],
                fields=['partner_id', 'amount_total:sum'],
                groupby=['partner_id'],
            )
            for item in po_data:
                pid = item['partner_id'][0]
                purchase_stats[pid] = {
                    'total_purchased': item['amount_total'] or 0.0,
                }

            # Outstanding bills (posted but not paid)
            bill_data = self.env['account.move'].read_group(
                domain=[
                    ('partner_id', 'in', supplier_ids),
                    ('move_type', '=', 'in_invoice'),
                    ('state', '=', 'posted'),
                    ('payment_state', 'in', ('not_paid', 'partial')),
                ],
                fields=['partner_id', 'amount_residual:sum'],
                groupby=['partner_id'],
            )
            for item in bill_data:
                pid = item['partner_id'][0]
                if pid not in purchase_stats:
                    purchase_stats[pid] = {'total_purchased': 0.0}
                purchase_stats[pid]['outstanding'] = item['amount_residual'] or 0.0

            # Overdue bills
            overdue_data = self.env['account.move'].read_group(
                domain=[
                    ('partner_id', 'in', supplier_ids),
                    ('move_type', '=', 'in_invoice'),
                    ('state', '=', 'posted'),
                    ('payment_state', 'in', ('not_paid', 'partial')),
                    ('invoice_date_due', '<', fields.Date.today()),
                ],
                fields=['partner_id', 'amount_residual:sum'],
                groupby=['partner_id'],
            )
            for item in overdue_data:
                pid = item['partner_id'][0]
                if pid not in purchase_stats:
                    purchase_stats[pid] = {'total_purchased': 0.0, 'outstanding': 0.0}
                purchase_stats[pid]['overdue'] = item['amount_residual'] or 0.0

        suppliers_list = []
        for p in partners:
            pid = p.id
            stats = purchase_stats.get(pid, {})
            payment_term = p.property_supplier_payment_term_id
            suppliers_list.append({
                'id': pid,
                'name': p.name or '',
                'email': p.email or '',
                'phone': p.phone or p.mobile or '',
                'vat': p.vat or '',
                'street': p.street or '',
                'city': p.city or '',
                'country_id': [p.country_id.id, p.country_id.name] if p.country_id else False,
                'property_supplier_payment_term_id': [payment_term.id, payment_term.name] if payment_term else False,
                'total_purchased': stats.get('total_purchased', 0.0),
                'outstanding': stats.get('outstanding', 0.0),
                'overdue': stats.get('overdue', 0.0),
                'supplier_rank': p.supplier_rank,
            })

        return {
            'success': True,
            'totalItems': total_count,
            'totalPages': total_pages,
            'currentPage': page,
            'itemsPerPage': limit,
            'data': suppliers_list,
        }

    @api.model
    def get_supplier_detail(self, supplier_id):
        """Get detailed supplier info including purchase history and bills."""
        partner = self.browse(int(supplier_id))
        if not partner.exists() or partner.supplier_rank <= 0:
            return {'success': False, 'message': 'Supplier not found'}

        # Purchase Orders
        pos = self.env['purchase.order'].search([
            ('partner_id', '=', partner.id),
        ], order='date_order desc')
        po_list = [{
            'id': po.id,
            'name': po.name,
            'date_order': po.date_order.strftime('%Y-%m-%d') if po.date_order else '',
            'amount_total': po.amount_total,
            'state': po.state,
            'billing_status': getattr(po, 'billing_status', ''),
        } for po in pos]

        # Vendor Bills
        bills = self.env['account.move'].search([
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'in_invoice'),
        ], order='invoice_date desc')
        bill_list = [{
            'id': b.id,
            'name': b.name,
            'invoice_date': b.invoice_date.strftime('%Y-%m-%d') if b.invoice_date else '',
            'invoice_date_due': b.invoice_date_due.strftime('%Y-%m-%d') if b.invoice_date_due else '',
            'amount_total': b.amount_total,
            'amount_residual': b.amount_residual,
            'state': b.state,
            'payment_state': b.payment_state,
        } for b in bills]

        # Payments made to this supplier
        payments = self.env['account.payment'].search([
            ('partner_id', '=', partner.id),
            ('partner_type', '=', 'supplier'),
            ('state', '=', 'posted'),
        ], order='date desc')
        payment_list = [{
            'id': pay.id,
            'name': pay.name,
            'date': pay.date.strftime('%Y-%m-%d') if pay.date else '',
            'amount': pay.amount,
            'journal_id': [pay.journal_id.id, pay.journal_id.name] if pay.journal_id else False,
        } for pay in payments]

        payment_term = partner.property_supplier_payment_term_id

        return {
            'success': True,
            'data': {
                'id': partner.id,
                'name': partner.name,
                'email': partner.email,
                'phone': partner.phone,
                'mobile': partner.mobile,
                'vat': partner.vat,
                'street': partner.street,
                'street2': partner.street2,
                'city': partner.city,
                'state_id': [partner.state_id.id, partner.state_id.name] if partner.state_id else False,
                'zip': partner.zip,
                'country_id': [partner.country_id.id, partner.country_id.name] if partner.country_id else False,
                'property_supplier_payment_term_id': [payment_term.id, payment_term.name] if payment_term else False,
                'supplier_rank': partner.supplier_rank,
                'purchase_orders': po_list,
                'vendor_bills': bill_list,
                'payments': payment_list,
            }
        }