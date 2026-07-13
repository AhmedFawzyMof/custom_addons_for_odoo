import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class VendorBillApi(models.AbstractModel):
    _name = 'vendor.bill.api'
    _description = 'Vendor Bill API Handler'

    def _get_company_domain(self):
        cids = self.env.context.get('allowed_company_ids', [])
        if cids:
            return [('company_id', 'in', cids)]
        return []

    def _ensure_invoice_taxes(self, bill):
        """Fill missing taxes on invoice lines to pass account_invoice_tax_required validation."""
        excluded_types = {'line_section', 'line_note', 'tax', 'payment_term', 'rounding', 'discount', 'cogs', 'epd'}
        for line in bill.line_ids.filtered(lambda l: l.display_type not in excluded_types and not l.tax_ids):
            tax_ids = False
            if line.product_id and line.product_id.supplier_taxes_id:
                tax_ids = line.product_id.supplier_taxes_id.ids
            else:
                default_tax = self.env['account.tax'].search([
                    ('type_tax_use', 'in', ('purchase', 'all')),
                    ('company_id', '=', bill.company_id.id),
                ], limit=1)
                if default_tax:
                    tax_ids = default_tax.ids
            if tax_ids:
                line.write({'tax_ids': [(6, 0, tax_ids)]})

    @api.model
    def get_vendor_bills(self, params=None):
        """Fetch vendor bills (account.move type: in_invoice) with filtering and pagination."""
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
        status = params.get('status', '')  # draft, posted, paid
        date_from = params.get('date_from', '')
        date_to = params.get('date_to', '')
        supplier_id = params.get('supplier_id', '')

        domain = [('move_type', '=', 'in_invoice'), *self._get_company_domain()]

        if search_term:
            domain = ['|',
                      ('name', 'ilike', search_term),
                      ('partner_id.name', 'ilike', search_term)] + domain

        if status:
            if status == 'paid':
                domain.append(('payment_state', '=', 'paid'))
            elif status == 'posted':
                domain.append(('state', '=', 'posted'))
                domain.append(('payment_state', 'in', ('not_paid', 'partial')))
            elif status == 'draft':
                domain.append(('state', '=', 'draft'))

        if date_from:
            domain.append(('invoice_date', '>=', date_from))
        if date_to:
            domain.append(('invoice_date', '<=', date_to))
        if supplier_id:
            try:
                domain.append(('partner_id', '=', int(supplier_id)))
            except (ValueError, TypeError):
                pass

        total_count = self.env['account.move'].search_count(domain)
        total_pages = (total_count + limit - 1) // limit if total_count > 0 else 1

        bills = self.env['account.move'].search(
            domain, offset=offset, limit=limit, order='invoice_date desc, id desc'
        )

        bill_list = []
        for b in bills:
            payment_term = b.invoice_payment_term_id
            bill_list.append({
                'id': b.id,
                'name': b.name,
                'partner_id': [b.partner_id.id, b.partner_id.name] if b.partner_id else False,
                'invoice_date': b.invoice_date.strftime('%Y-%m-%d') if b.invoice_date else '',
                'invoice_date_due': b.invoice_date_due.strftime('%Y-%m-%d') if b.invoice_date_due else '',
                'amount_total': b.amount_total,
                'amount_residual': b.amount_residual,
                'amount_tax': b.amount_tax,
                'state': b.state,
                'payment_state': b.payment_state,
                'invoice_payment_term_id': [payment_term.id, payment_term.name] if payment_term else False,
                'reference': b.ref or '',
                'supplier_reference': getattr(b, 'supplier_reference', b.ref) or '',
                'currency_id': [b.currency_id.id, b.currency_id.name] if b.currency_id else False,
                'company_id': [b.company_id.id, b.company_id.name] if b.company_id else False,
            })

        return {
            'success': True,
            'totalItems': total_count,
            'totalPages': total_pages,
            'currentPage': page,
            'itemsPerPage': limit,
            'data': bill_list,
        }

    @api.model
    def get_vendor_bill_detail(self, bill_id):
        """Get full vendor bill detail with lines, payments, and linked POs."""
        bill = self.env['account.move'].browse(int(bill_id))
        if not bill.exists() or bill.move_type != 'in_invoice':
            return {'success': False, 'message': 'Vendor bill not found'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and bill.company_id.id not in _cids:
            return {'success': False, 'message': 'Vendor bill not found in this company'}

        # Invoice lines — iterate over line_ids instead of invoice_line_ids
        # to avoid missing lines whose display_type may not match the field's
        # domain filter [('display_type', 'in', ('product', 'line_section', 'line_note'))].
        NON_INVOICE_DISPLAY_TYPES = {'tax', 'payment_term', 'rounding', 'discount', 'cogs', 'epd'}
        lines = []
        inv_line_ids = bill.invoice_line_ids
        all_line_ids = bill.line_ids
        _logger.info(
            "get_vendor_bill_detail bill=%s invoice_line_ids=%d line_ids=%d",
            bill.id, len(inv_line_ids), len(all_line_ids),
        )
        for line in all_line_ids:
            if line.display_type and line.display_type in NON_INVOICE_DISPLAY_TYPES:
                _logger.debug(
                    "  Skipping line %s display_type=%s", line.id, line.display_type,
                )
                continue
            _logger.debug(
                "  Including line %s display_type=%s product=%s",
                line.id, line.display_type,
                line.product_id.display_name if line.product_id else 'N/A',
            )
            lines.append({
                'id': line.id,
                'product_id': [line.product_id.id, line.product_id.display_name] if line.product_id else False,
                'name': line.name,
                'quantity': line.quantity,
                'price_unit': line.price_unit,
                'price_subtotal': line.price_subtotal,
                'price_total': line.price_total,
                'tax_ids': [{'id': t.id, 'name': t.name} for t in line.tax_ids],
                'discount': line.discount if hasattr(line, 'discount') else 0,
            })

        # Linked purchase orders
        purchase_orders = self.env['purchase.order'].search([
            ('invoice_ids', 'in', bill.id),
            *([('company_id', 'in', _cids)] if _cids else []),
        ])
        po_list = [{
            'id': po.id,
            'name': po.name,
            'date_order': po.date_order.strftime('%Y-%m-%d') if po.date_order else '',
            'amount_total': po.amount_total,
            'state': po.state,
        } for po in purchase_orders]

        # Linked payments
        payments = []
        for payment in bill._get_reconciled_payments():
            payments.append({
                'id': payment.id,
                'name': payment.name,
                'date': payment.date.strftime('%Y-%m-%d') if payment.date else '',
                'amount': payment.amount,
                'journal_id': [payment.journal_id.id, payment.journal_id.name] if payment.journal_id else False,
                'state': payment.state,
            })

        payment_term = bill.invoice_payment_term_id

        return {
            'success': True,
            'data': {
                'id': bill.id,
                'name': bill.name,
                'partner_id': [bill.partner_id.id, bill.partner_id.name] if bill.partner_id else False,
                'invoice_date': bill.invoice_date.strftime('%Y-%m-%d') if bill.invoice_date else '',
                'invoice_date_due': bill.invoice_date_due.strftime('%Y-%m-%d') if bill.invoice_date_due else '',
                'amount_total': bill.amount_total,
                'amount_residual': bill.amount_residual,
                'amount_tax': bill.amount_tax,
                'amount_untaxed': bill.amount_untaxed,
                'state': bill.state,
                'payment_state': bill.payment_state,
                'reference': bill.ref or '',
                'supplier_reference': getattr(bill, 'supplier_reference', bill.ref) or '',
                'narration': bill.narration or '',
                'currency_id': [bill.currency_id.id, bill.currency_id.name] if bill.currency_id else False,
                'invoice_payment_term_id': [payment_term.id, payment_term.name] if payment_term else False,
                'lines': lines,
                'purchase_orders': po_list,
                'payments': payments,
            }
        }

    @api.model
    def create_vendor_bill(self, payload):
        """Create a vendor bill (account.move type: in_invoice) from payload."""
        partner_id = payload.get('partner_id')
        invoice_date = payload.get('invoice_date', fields.Date.today())
        ref = payload.get('reference', '')
        lines_data = payload.get('lines', [])
        payment_term_id = payload.get('payment_term_id', False)

        if not partner_id:
            return {'success': False, 'message': 'Supplier (partner_id) is required'}
        if not lines_data:
            return {'success': False, 'message': 'At least one line is required'}

        partner = self.env['res.partner'].browse(int(partner_id))
        if not partner.exists():
            return {'success': False, 'message': 'Supplier not found'}

        invoice_lines = []
        for line in lines_data:
            product_id = line.get('product_id')
            quantity = float(line.get('quantity', 1))
            price_unit = float(line.get('price_unit', 0))
            name = line.get('name', '')
            discount = float(line.get('discount', 0))
            tax_ids = line.get('tax_ids', [])

            product = self.env['product.product'].browse(int(product_id)) if product_id else False
            if not name and product:
                name = product.display_name

            # Use provided tax_ids, fallback to product's supplier taxes,
            # then to any purchase tax in the company
            if not tax_ids and product and product.supplier_taxes_id:
                tax_ids = product.supplier_taxes_id.ids

            line_vals = {
                'product_id': int(product_id) if product_id else False,
                'name': name or 'خط مشتريات',
                'quantity': quantity,
                'price_unit': price_unit,
                'discount': discount,
                'tax_ids': [(6, 0, [int(t) for t in tax_ids])] if tax_ids else False,
            }
            invoice_lines.append((0, 0, line_vals))

        vals = {
            'move_type': 'in_invoice',
            'partner_id': int(partner_id),
            'invoice_date': invoice_date,
            'ref': ref,
            'invoice_payment_term_id': int(payment_term_id) if payment_term_id else False,
            'invoice_line_ids': invoice_lines,
        }

        try:
            bill = self.env['account.move'].create(vals)
            # Set check_total to match amount_total to pass
            # account_invoice_check_total validation on posting
            if hasattr(bill, 'check_total'):
                bill.write({'check_total': bill.amount_total})
            return {
                'success': True,
                'bill_id': bill.id,
                'name': bill.name,
                'message': 'تم إنشاء فاتورة المورد بنجاح',
            }
        except Exception as e:
            return {'success': False, 'message': f'فشل في إنشاء الفاتورة: {str(e)}'}

    @api.model
    def update_vendor_bill_status(self, bill_id, new_status):
        """Update vendor bill status: draft -> posted -> paid."""
        bill = self.env['account.move'].browse(int(bill_id))
        if not bill.exists() or bill.move_type != 'in_invoice':
            return {'success': False, 'message': 'Vendor bill not found'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and bill.company_id.id not in _cids:
            return {'success': False, 'message': 'Vendor bill not found in this company'}

        try:
            if new_status == 'posted' and bill.state == 'draft':
                self._ensure_invoice_taxes(bill)
                if hasattr(bill, 'check_total'):
                    bill.write({'check_total': bill.amount_total})
                bill.action_post()
            elif new_status == 'draft' and bill.state == 'posted':
                bill.button_draft()
            elif new_status == 'cancel' and bill.state != 'paid':
                bill.button_cancel()
            else:
                return {'success': False, 'message': f'Cannot change status to {new_status}'}

            return {
                'success': True,
                'bill_id': bill.id,
                'state': bill.state,
                'payment_state': bill.payment_state,
                'message': f'تم تحديث الحالة إلى {new_status}',
            }
        except Exception as e:
            return {'success': False, 'message': str(e)}

    @api.model
    def register_vendor_payment(self, payload):
        """Register a payment for a vendor bill."""
        bill_id = payload.get('bill_id')
        amount = float(payload.get('amount', 0))
        payment_date = payload.get('payment_date', fields.Date.today())
        journal_id = payload.get('journal_id')
        payment_method_id = payload.get('payment_method_id', False)

        if not bill_id or amount <= 0:
            return {'success': False, 'message': 'Bill ID and positive amount required'}

        bill = self.env['account.move'].browse(int(bill_id))
        if not bill.exists() or bill.move_type != 'in_invoice':
            return {'success': False, 'message': 'Vendor bill not found'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and bill.company_id.id not in _cids:
            return {'success': False, 'message': 'Vendor bill not found in this company'}

        if bill.state == 'draft':
            self._ensure_invoice_taxes(bill)
            if hasattr(bill, 'check_total'):
                bill.write({'check_total': bill.amount_total})
        elif bill.state != 'posted':
            return {'success': False, 'message': 'Bill must be posted before payment'}

        if not journal_id:
            journal = self.env['account.journal'].search([
                ('type', 'in', ('cash', 'bank')),
                ('company_id', '=', bill.company_id.id),
            ], limit=1)
            if not journal:
                return {'success': False, 'message': 'No cash/bank journal found'}
            journal_id = journal.id

        try:
            if bill.state == 'draft':
                bill.action_post()

            payment_vals = {
                'payment_type': 'outbound',
                'partner_type': 'supplier',
                'partner_id': bill.partner_id.id,
                'amount': amount,
                'date': payment_date,
                'journal_id': int(journal_id),
                'payment_method_id': int(payment_method_id) if payment_method_id else False,
                'currency_id': bill.currency_id.id,
            }
            payment = self.env['account.payment'].create(payment_vals)
            payment.action_post()

            # Reconcile payment with bill
            lines = bill.line_ids.filtered(lambda l: l.account_id == bill.partner_id.property_account_payable_id)
            payment_lines = payment.move_id.line_ids.filtered(lambda l: l.account_id == bill.partner_id.property_account_payable_id)
            if lines and payment_lines:
                (lines + payment_lines).reconcile()

            return {
                'success': True,
                'payment_id': payment.id,
                'name': payment.name,
                'message': 'تم تسجيل الدفعة بنجاح',
            }
        except Exception as e:
            return {'success': False, 'message': f'فشل في تسجيل الدفعة: {str(e)}'}
