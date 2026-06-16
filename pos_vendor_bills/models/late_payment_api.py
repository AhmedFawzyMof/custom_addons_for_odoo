from odoo import models, fields, api
from datetime import date, datetime


class LatePaymentApi(models.AbstractModel):
    _name = 'late.payment.api'
    _description = 'Late Payment API Handler'

    def _get_company_domain(self):
        cids = self.env.context.get('allowed_company_ids', [])
        if cids:
            return [('company_id', 'in', cids)]
        return []

    @api.model
    def get_late_payments(self, params=None):
        if params is None:
            params = {}
        try:
            page = max(1, int(params.get('page', 1)))
            limit = max(1, int(params.get('limit', 20)))
        except (ValueError, TypeError):
            page = 1
            limit = 20
        offset = (page - 1) * limit
        search_term = params.get('search', '').strip()
        type_filter = params.get('type', '')  # 'bill', 'po', or '' for both
        date_from = params.get('date_from', '')
        date_to = params.get('date_to', '')
        aging_bucket = params.get('aging_bucket', '')  # '0-30', '30-60', '60-90', '90+'
        supplier_id = params.get('supplier_id', '')

        today = date.today()
        company_domain = self._get_company_domain()

        late_items = []

        # 1. Late Vendor Bills
        if not type_filter or type_filter == 'bill':
            bill_domain = [
                ('move_type', '=', 'in_invoice'),
                ('state', '=', 'posted'),
                ('payment_state', '!=', 'paid'),
                ('invoice_date_due', '<', today),
            ] + company_domain

            if search_term:
                bill_domain = ['|', ('name', 'ilike', search_term),
                               ('partner_id.name', 'ilike', search_term)] + bill_domain
            if supplier_id:
                try:
                    bill_domain.append(('partner_id', '=', int(supplier_id)))
                except (ValueError, TypeError):
                    pass
            if date_from:
                bill_domain.append(('invoice_date_due', '>=', date_from))
            if date_to:
                bill_domain.append(('invoice_date_due', '<=', date_to))

            bills = self.env['account.move'].search(bill_domain, order='invoice_date_due asc')
            for b in bills:
                days_overdue = (today - b.invoice_date_due).days if b.invoice_date_due else 0
                bucket = self._get_aging_bucket(days_overdue)
                if aging_bucket and bucket != aging_bucket:
                    continue
                late_items.append({
                    'id': b.id,
                    'type': 'bill',
                    'reference': b.name,
                    'partner_id': [b.partner_id.id, b.partner_id.name] if b.partner_id else False,
                    'date': b.invoice_date.strftime('%Y-%m-%d') if b.invoice_date else '',
                    'date_due': b.invoice_date_due.strftime('%Y-%m-%d') if b.invoice_date_due else '',
                    'amount_total': b.amount_total,
                    'amount_residual': b.amount_residual,
                    'days_overdue': days_overdue,
                    'aging_bucket': bucket,
                    'state': b.state,
                    'payment_state': b.payment_state,
                })

        # 2. Late Purchase Orders (confirmed but not fully received past expected date)
        if not type_filter or type_filter == 'po':
            po_domain = [
                ('state', '=', 'purchase'),
                ('receipt_status', 'in', ('pending', 'partial')),
            ] + company_domain

            if search_term:
                po_domain = ['|', ('name', 'ilike', search_term),
                             ('partner_id.name', 'ilike', search_term)] + po_domain
            if supplier_id:
                try:
                    po_domain.append(('partner_id', '=', int(supplier_id)))
                except (ValueError, TypeError):
                    pass

            pos = self.env['purchase.order'].search(po_domain, order='date_order asc')
            for po in pos:
                date_planned = po.order_line and min(
                    (l.date_planned.date() for l in po.order_line if l.date_planned),
                    default=None
                )
                days_overdue = 0
                if date_planned and date_planned < today:
                    days_overdue = (today - date_planned).days
                elif not date_planned and po.date_order:
                    po_date = po.date_order.date() if hasattr(po.date_order, 'date') else po.date_order
                    if po_date < today:
                        days_overdue = (today - po_date).days

                if days_overdue <= 0:
                    continue

                bucket = self._get_aging_bucket(days_overdue)
                if aging_bucket and bucket != aging_bucket:
                    continue
                if date_from and date_planned and date_planned < datetime.strptime(date_from, '%Y-%m-%d').date():
                    continue
                if date_to and date_planned and date_planned > datetime.strptime(date_to, '%Y-%m-%d').date():
                    continue

                receipt_status_raw = po.receipt_status
                receipt_status_map = {'full': 'done', 'partial': 'partial'}
                receipt_status = receipt_status_map.get(receipt_status_raw, 'pending')

                late_items.append({
                    'id': po.id,
                    'type': 'po',
                    'reference': po.name,
                    'partner_id': [po.partner_id.id, po.partner_id.name] if po.partner_id else False,
                    'date': po.date_order.strftime('%Y-%m-%d') if po.date_order else '',
                    'date_due': date_planned.strftime('%Y-%m-%d') if date_planned else '',
                    'amount_total': po.amount_total,
                    'amount_residual': po.amount_total,
                    'days_overdue': days_overdue,
                    'aging_bucket': bucket,
                    'state': po.state,
                    'receipt_status': receipt_status,
                    'payment_state': '',
                })

        # Sort by days_overdue desc
        late_items.sort(key=lambda x: x['days_overdue'], reverse=True)

        total_count = len(late_items)
        total_pages = (total_count + limit - 1) // limit if total_count > 0 else 1
        paged_items = late_items[offset:offset + limit]

        total_overdue_amount = sum(item['amount_residual'] for item in late_items)

        return {
            'success': True,
            'totalItems': total_count,
            'totalPages': total_pages,
            'currentPage': page,
            'itemsPerPage': limit,
            'data': paged_items,
            'summary': {
                'total_overdue_count': total_count,
                'total_overdue_amount': total_overdue_amount,
                'bill_count': sum(1 for i in late_items if i['type'] == 'bill'),
                'po_count': sum(1 for i in late_items if i['type'] == 'po'),
            },
        }

    @api.model
    def _get_aging_bucket(self, days):
        if days <= 30:
            return '0-30'
        elif days <= 60:
            return '30-60'
        elif days <= 90:
            return '60-90'
        else:
            return '90+'

    @api.model
    def get_late_payment_summary(self):
        today = date.today()
        company_domain = self._get_company_domain()

        bill_domain = [
            ('move_type', '=', 'in_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', '!=', 'paid'),
            ('invoice_date_due', '<', today),
        ] + company_domain
        bills = self.env['account.move'].search(bill_domain)
        bill_count = len(bills)
        bill_total = sum(b.amount_residual for b in bills)

        po_domain = [
            ('state', '=', 'purchase'),
            ('receipt_status', 'in', ('pending', 'partial')),
        ] + company_domain
        pos = self.env['purchase.order'].search(po_domain)
        po_count = 0
        po_total = 0.0
        for po in pos:
            date_planned = po.order_line and min(
                (l.date_planned.date() for l in po.order_line if l.date_planned),
                default=None
            )
            if date_planned and date_planned < today:
                po_count += 1
                po_total += po.amount_total
            elif not date_planned and po.date_order:
                po_date = po.date_order.date() if hasattr(po.date_order, 'date') else po.date_order
                if po_date < today:
                    po_count += 1
                    po_total += po.amount_total

        return {
            'success': True,
            'data': {
                'total_overdue': bill_count + po_count,
                'total_overdue_amount': bill_total + po_total,
                'bill_overdue_count': bill_count,
                'bill_overdue_amount': bill_total,
                'po_overdue_count': po_count,
                'po_overdue_amount': po_total,
            },
        }
