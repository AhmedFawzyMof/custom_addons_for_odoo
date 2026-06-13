import logging

from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_is_zero

_logger = logging.getLogger(__name__)


class PurchaseOrderApi(models.AbstractModel):
    _name = 'purchase.order.api'
    _description = 'Purchase Order API Handler'

    def _get_company_domain(self):
        cids = self.env.context.get('allowed_company_ids', [])
        if cids:
            return [('company_id', 'in', cids)]
        return []

    @api.model
    def get_purchase_orders(self, params=None):
        """Fetch purchase orders with filtering and pagination."""
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
        state = params.get('state', '')  # draft, sent, purchase, done, cancel
        date_from = params.get('date_from', '')
        date_to = params.get('date_to', '')
        supplier_id = params.get('supplier_id', '')

        domain = self._get_company_domain()
        if search_term:
            domain = ['|',
                      ('name', 'ilike', search_term),
                      ('partner_id.name', 'ilike', search_term)] + domain

        if state:
            domain.append(('state', '=', state))
        if date_from:
            domain.append(('date_order', '>=', date_from))
        if date_to:
            domain.append(('date_order', '<=', date_to + ' 23:59:59'))
        if supplier_id:
            try:
                domain.append(('partner_id', '=', int(supplier_id)))
            except (ValueError, TypeError):
                pass

        total_count = self.env['purchase.order'].search_count(domain)
        total_pages = (total_count + limit - 1) // limit if total_count > 0 else 1

        orders = self.env['purchase.order'].search(
            domain, offset=offset, limit=limit, order='date_order desc, id desc'
        )

        po_list = []
        for po in orders:
            receipt_status_raw = po.receipt_status
            receipt_status_map = {'full': 'done', 'partial': 'partial'}
            receipt_status = receipt_status_map.get(receipt_status_raw, 'pending')

            po_list.append({
                'id': po.id,
                'name': po.name,
                'partner_id': [po.partner_id.id, po.partner_id.name] if po.partner_id else False,
                'date_order': po.date_order.strftime('%Y-%m-%d') if po.date_order else '',
                'amount_total': po.amount_total,
                'state': po.state,
                'receipt_status': receipt_status,
                'order_line_count': len(po.order_line),
                'currency_id': [po.currency_id.id, po.currency_id.name] if po.currency_id else False,
                'partner_ref': po.partner_ref or '',
            })

        return {
            'success': True,
            'totalItems': total_count,
            'totalPages': total_pages,
            'currentPage': page,
            'itemsPerPage': limit,
            'data': po_list,
        }

    @api.model
    def get_purchase_order_detail(self, po_id):
        """Get full purchase order detail including lines and stock pickings."""
        po = self.env['purchase.order'].browse(int(po_id))
        if not po.exists():
            return {'success': False, 'message': 'Purchase order not found'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and po.company_id.id not in _cids:
            return {'success': False, 'message': 'Purchase order not found in this company'}

        lines = []
        for line in po.order_line:
            lines.append({
                'id': line.id,
                'product_id': [line.product_id.id, line.product_id.display_name] if line.product_id else False,
                'name': line.name,
                'product_qty': line.product_qty,
                'qty_received': line.qty_received,
                'qty_invoiced': line.qty_invoiced,
                'price_unit': line.price_unit,
                'price_subtotal': line.price_subtotal,
                'price_total': line.price_total,
                'date_planned': line.date_planned.strftime('%Y-%m-%d') if line.date_planned else '',
                'tax_ids': [{'id': t.id, 'name': t.name} for t in line.taxes_id],
            })

        pickings = []
        for p in po.picking_ids:
            move_lines = []
            for ml in p.move_line_ids:
                move_lines.append({
                    'product_id': [ml.product_id.id, ml.product_id.display_name] if ml.product_id else False,
                    'qty_done': ml.qty_done,
                    'quantity': ml.quantity if hasattr(ml, 'quantity') else ml.qty_done,
                })
            pickings.append({
                'id': p.id,
                'name': p.name,
                'state': p.state,
                'scheduled_date': p.scheduled_date.strftime('%Y-%m-%d') if p.scheduled_date else '',
                'move_lines': move_lines,
            })

        # Related vendor bills
        bills = self.env['account.move'].search([
            ('invoice_origin', '=', po.name),
            ('move_type', '=', 'in_invoice'),
            *([('company_id', 'in', _cids)] if _cids else []),
        ])
        bill_list = [{
            'id': b.id,
            'name': b.name,
            'state': b.state,
            'payment_state': b.payment_state,
            'amount_total': b.amount_total,
            'invoice_date': b.invoice_date.strftime('%Y-%m-%d') if b.invoice_date else '',
        } for b in bills]

        return {
            'success': True,
            'data': {
                'id': po.id,
                'name': po.name,
                'partner_id': [po.partner_id.id, po.partner_id.name] if po.partner_id else False,
                'date_order': po.date_order.strftime('%Y-%m-%d') if po.date_order else '',
                'date_approve': po.date_approve.strftime('%Y-%m-%d') if po.date_approve else '',
                'amount_total': po.amount_total,
                'amount_untaxed': po.amount_untaxed,
                'state': po.state,
                'partner_ref': po.partner_ref or '',
                'notes': po.notes or '',
                'currency_id': [po.currency_id.id, po.currency_id.name] if po.currency_id else False,
                'lines': lines,
                'pickings': pickings,
                'vendor_bills': bill_list,
            }
        }

    @api.model
    def create_purchase_order(self, payload):
        """Create a purchase order with lines."""
        partner_id = payload.get('partner_id')
        lines_data = payload.get('lines', [])
        date_order = payload.get('date_order', fields.Date.today())
        payment_term_id = payload.get('payment_term_id', False)
        notes = payload.get('notes', '')

        if not partner_id:
            return {'success': False, 'message': 'Supplier (partner_id) is required'}
        if not lines_data:
            return {'success': False, 'message': 'At least one line is required'}

        order_lines = []
        for line in lines_data:
            product_id = line.get('product_id')
            quantity = float(line.get('quantity', 1))
            price_unit = float(line.get('price_unit', 0))

            if not product_id:
                continue

            product = self.env['product.product'].browse(int(product_id))
            name = line.get('name', product.display_name)
            tax_ids = line.get('tax_ids', [(6, 0, product.supplier_taxes_id.ids)]) if product.supplier_taxes_id else []

            order_lines.append((0, 0, {
                'product_id': int(product_id),
                'name': name,
                'product_qty': quantity,
                'price_unit': price_unit,
                'price_subtotal': quantity * price_unit,
                'product_uom': product.uom_po_id.id or product.uom_id.id,
                'taxes_id': tax_ids if isinstance(tax_ids, list) else [(6, 0, tax_ids)],
                'date_planned': fields.Datetime.now(),
            }))

        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'incoming'),
            ('warehouse_id.company_id', '=', self.env.company.id)
        ], limit=1)
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'incoming'),
                ('warehouse_id', '=', False)
            ], limit=1)

        vals = {
            'partner_id': int(partner_id),
            'date_order': date_order,
            'order_line': order_lines,
            'notes': notes,
            'picking_type_id': picking_type.id if picking_type else False,
            'company_id': self.env.company.id,
        }

        if payment_term_id:
            vals['payment_term_id'] = int(payment_term_id)

        try:
            po = self.env['purchase.order'].create(vals)
            return {
                'success': True,
                'po_id': po.id,
                'name': po.name,
                'message': 'تم إنشاء أمر الشراء بنجاح',
            }
        except Exception as e:
            return {'success': False, 'message': f'فشل في إنشاء أمر الشراء: {str(e)}'}

    @api.model
    def confirm_purchase_order(self, po_id):
        """Confirm a purchase order (draft -> purchase)."""
        po = self.env['purchase.order'].browse(int(po_id))
        if not po.exists():
            return {'success': False, 'message': 'Purchase order not found'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and po.company_id.id not in _cids:
            return {'success': False, 'message': 'Purchase order not found in this company'}
        if po.state != 'draft':
            return {'success': False, 'message': f'Cannot confirm PO in state: {po.state}'}

        try:
            po.button_confirm()
            return {
                'success': True,
                'po_id': po.id,
                'state': po.state,
                'message': 'تم تأكيد أمر الشراء بنجاح',
            }
        except Exception as e:
            return {'success': False, 'message': f'فشل في تأكيد أمر الشراء: {str(e)}'}

    @api.model
    def receive_purchase_order(self, payload):
        """Receive stock for a purchase order with optional partial quantities."""
        po_id = payload.get('po_id')
        lines_data = payload.get('lines', [])

        if not po_id:
            return {'success': False, 'message': 'Purchase order ID required'}

        po = self.env['purchase.order'].browse(int(po_id))
        if not po.exists():
            return {'success': False, 'message': 'Purchase order not found'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and po.company_id.id not in _cids:
            return {'success': False, 'message': 'Purchase order not found in this company'}
        if po.state not in ('purchase', 'done'):
            return {'success': False, 'message': f'PO must be confirmed first (state: {po.state})'}

        try:
            pickings = po.picking_ids
            if not pickings:
                return {'success': False, 'message': 'No pickings found. Try confirming the PO first.'}

            _logger.info(">>> RECEIVE PO=%s state=%s pickings=%s", po.id, po.state, pickings.ids)
            for line in po.order_line:
                _logger.info(
                    "  LINE %s: product=%s type=%s purchase_method=%s qty_received_method=%s moves=%s",
                    line.id, line.product_id.id, line.product_id.type,
                    line.product_id.purchase_method, line.qty_received_method,
                    line.move_ids.ids)
                for m in line.move_ids:
                    _logger.info(
                        "    MOVE %s: state=%s qty=%s picking=%s pick_state=%s",
                        m.id, m.state, m.quantity, m.picking_id.id, m.picking_id.state)
            _logger.info("  PO invoice_status=%s", po.invoice_status)

            for picking in pickings:
                if picking.state == 'done':
                    continue

                if picking.state == 'cancel':
                    picking.state = 'draft'
                    picking.move_ids.write({'state': 'draft'})
                    picking.move_ids._action_confirm()._action_assign()
                    picking.move_ids.write({'picked': True})

                for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                    qty_to_receive = move.product_uom_qty
                    if lines_data:
                        for ld in lines_data:
                            pid = ld.get('product_id')
                            if pid and int(pid) == move.product_id.id:
                                qty_to_receive = float(ld.get('qty_received', ld.get('quantity', qty_to_receive)))
                                break
                    move.quantity = qty_to_receive

                if picking.state != 'done':
                    picking.with_context(cancel_backorder=True)._action_done()

            for line in po.order_line:
                received = 0.0
                for move in line.move_ids.filtered(lambda m: m.state == 'done'):
                    received += move.product_uom._compute_quantity(move.quantity, line.product_uom)
                if received:
                    line.qty_received = received

            po.order_line._compute_qty_invoiced()
            po._get_invoiced()

            po.invalidate_model(['receipt_status'])

            _logger.info(">>> AFTER RECEIVE PO=%s invoice_status=%s", po.id, po.invoice_status)
            for line in po.order_line:
                _logger.info(
                    "  LINE %s: qty_received=%s qty_to_invoice=%s qty_invoiced=%s",
                    line.id, line.qty_received, line.qty_to_invoice, line.qty_invoiced)

            receipt_status_raw = po.receipt_status
            receipt_status_map = {'full': 'done', 'partial': 'partial'}
            return {
                'success': True,
                'message': 'تم استلام المنتجات بنجاح',
                'receipt_status': receipt_status_map.get(receipt_status_raw, 'pending'),
            }
        except Exception as e:
            return {'success': False, 'message': f'فشل في استلام المنتجات: {str(e)}'}

    @api.model
    def create_bill_from_po(self, po_id):
        """Generate a vendor bill from a purchase order (3-way match)."""
        po = self.env['purchase.order'].browse(int(po_id))
        if not po.exists():
            return {'success': False, 'message': 'Purchase order not found'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and po.company_id.id not in _cids:
            return {'success': False, 'message': 'Purchase order not found in this company'}
        if po.state not in ('purchase', 'done'):
            return {'success': False, 'message': 'PO must be confirmed to create a bill'}

        try:
            _logger.info(">>> CREATE BILL PO=%s state=%s", po.id, po.state)
            for line in po.order_line:
                _logger.info(
                    "  LINE %s: product=%s type=%s purchase_method=%s qty_received=%s qty_to_invoice=%s qty_invoiced=%s moves=%s",
                    line.id, line.product_id.id, line.product_id.type,
                    line.product_id.purchase_method, line.qty_received,
                    line.qty_to_invoice, line.qty_invoiced, line.move_ids.ids)
            _logger.info("  PO invoice_status=%s", po.invoice_status)

            po.order_line._compute_qty_invoiced()
            po._get_invoiced()

            _logger.info(">>> AFTER RECOMPUTE PO=%s invoice_status=%s", po.id, po.invoice_status)
            for line in po.order_line:
                _logger.info(
                    "  LINE %s: qty_received=%s qty_to_invoice=%s qty_invoiced=%s",
                    line.id, line.qty_received, line.qty_to_invoice, line.qty_invoiced)

            invoice_vals = po._prepare_invoice()
            invoice_vals['invoice_date'] = fields.Date.today()
            precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
            sequence = 10
            for line in po.order_line:
                if line.display_type == 'line_section' or float_is_zero(line.qty_to_invoice, precision_digits=precision):
                    continue
                line_vals = line._prepare_account_move_line()
                line_vals['sequence'] = sequence
                invoice_vals['invoice_line_ids'].append((0, 0, line_vals))
                sequence += 10

            if not invoice_vals['invoice_line_ids']:
                existing = po.invoice_ids.filtered(lambda inv: inv.state != 'cancel')
                if existing:
                    bill = existing[0]
                    if not bill.invoice_date:
                        bill.write({'invoice_date': fields.Date.today()})
                    return {
                        'success': True,
                        'bill_id': bill.id,
                        'name': bill.name,
                        'message': 'تم فوترة أمر الشراء مسبقاً.',
                    }
                return {'success': False, 'message': 'لا توجد بنود قابلة للفوترة في أمر الشراء هذا.'}

            bill = self.env['account.move'].with_context(default_move_type='in_invoice').create(invoice_vals)

            po.invalidate_model(['invoice_ids'])
            return {
                'success': True,
                'bill_id': bill.id,
                'name': bill.name,
                'message': 'تم إنشاء فاتورة المورد من أمر الشراء بنجاح',
            }
        except Exception as e:
            return {'success': False, 'message': f'فشل في إنشاء الفاتورة: {str(e)}'}

    @api.model
    def fix_missing_invoice_dates(self):
        """Backfill missing invoice_date on vendor bills created from POs."""
        bills = self.env['account.move'].search([
            ('move_type', '=', 'in_invoice'),
            ('invoice_date', '=', False),
        ])
        count = 0
        for bill in bills:
            bill.write({'invoice_date': fields.Date.today()})
            count += 1
        _logger.info("Fixed %s vendor bills with missing invoice_date", count)
        return {'success': True, 'fixed_count': count, 'message': f'تم إصلاح {count} فاتورة بدون تاريخ'}
