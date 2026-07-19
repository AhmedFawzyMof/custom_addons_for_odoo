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
                'amount_untaxed': po.amount_untaxed,
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
                'list_price': line.product_id.list_price if line.product_id else 0.0,
                'price_subtotal': line.price_subtotal,
                'price_total': line.price_total,
                'date_planned': line.date_planned.strftime('%Y-%m-%d') if line.date_planned else '',
                'tax_ids': [{'id': t.id, 'name': t.name} for t in line.taxes_id],
                'location_allocations': [{
                    'id': a.id,
                    'location_id': a.location_id.id,
                    'location_name': a.location_id.name,
                    'quantity': a.quantity,
                } for a in line.location_allocations],
            })

        pickings = []
        for p in po.picking_ids:
            move_lines = []
            for ml in p.move_line_ids:
                move_lines.append({
                    'product_id': [ml.product_id.id, ml.product_id.display_name] if ml.product_id else False,
                    'qty_done': getattr(ml, 'qty_done', 0),
                    'quantity': ml.quantity if hasattr(ml, 'quantity') else getattr(ml, 'qty_done', 0),
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
        active_lines_data = []
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
                'product_uom': product.uom_po_id.id if product.uom_po_id.category_id == product.uom_id.category_id else product.uom_id.id,
                'taxes_id': tax_ids if isinstance(tax_ids, list) else [(6, 0, tax_ids)],
                'date_planned': fields.Datetime.now(),
            }))
            active_lines_data.append(line)

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

            for line_data, po_line in zip(active_lines_data, po.order_line):
                for alloc in line_data.get('location_allocations', []):
                    self.env['purchase.order.line.location'].create({
                        'line_id': po_line.id,
                        'location_id': alloc['location_id'],
                        'quantity': alloc['quantity'],
                    })

            return {
                'success': True,
                'po_id': po.id,
                'name': po.name,
                'message': 'تم إنشاء أمر الشراء بنجاح',
            }
        except Exception as e:
            return {'success': False, 'message': f'فشل في إنشاء أمر الشراء: {str(e)}'}

    @api.model
    def update_purchase_order(self, payload):
        """Update a purchase order (draft only) with new header + lines."""
        po_id = payload.get('po_id')
        if not po_id:
            return {'success': False, 'message': 'Purchase order ID required'}

        po = self.env['purchase.order'].browse(int(po_id))
        if not po.exists():
            return {'success': False, 'message': 'Purchase order not found'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and po.company_id.id not in _cids:
            return {'success': False, 'message': 'Purchase order not found in this company'}
        pre_state = po.state
        is_done_edit = po.state == 'done'
        if po.state not in ('draft', 'sent', 'purchase', 'done'):
            return {'success': False, 'message': f'Cannot edit PO in state: {po.state}'}

        try:
            partner_id = payload.get('partner_id')
            date_order = payload.get('date_order')
            notes = payload.get('notes', '')
            lines_data = payload.get('lines', [])
            new_state = payload.get('state')
            new_receipt_status = payload.get('receipt_status')

            # --- Safety Checks for done state edits ---
            if is_done_edit and lines_data:
                existing_lines = {ol.id: ol for ol in po.order_line}
                incoming_ids = {int(ld.get('id')) for ld in lines_data if ld.get('id')}
                for ld in lines_data:
                    lid = ld.get('id')
                    if not lid:
                        continue
                    lid = int(lid)
                    ol = existing_lines.get(lid)
                    if not ol:
                        continue
                    new_product_id = ld.get('product_id')
                    if new_product_id and int(new_product_id) != ol.product_id.id:
                        if ol.qty_received or ol.qty_invoiced:
                            return {'success': False, 'message': (
                                'لا يمكن تغيير المنتج في بند تم استلامه أو فوترته بالفعل. '
                                'قم بإنشاء بند جديد بدلاً من ذلك.'
                            )}
                    if 'price_unit' in ld:
                        new_price = float(ld.get('price_unit', 0))
                        if abs(new_price - ol.price_unit) > 0.001 and ol.qty_invoiced:
                            return {'success': False, 'message': (
                                'لا يمكن تغيير سعر الشراء في بند تمت فوترته بالفعل.'
                            )}
                # Check deletion safety
                to_remove_ids = {lid for lid in existing_lines.keys()} - incoming_ids
                for lid in to_remove_ids:
                    ol = existing_lines[lid]
                    if ol.qty_received or ol.qty_invoiced:
                        return {'success': False, 'message': (
                            'لا يمكن حذف بند تم استلامه أو فوترته بالفعل. '
                            f'المنتج: {ol.product_id.display_name}'
                        )}

            # --- Capture pre-change state for logging ---
            pre_header = {
                'partner_id': po.partner_id.id if po.partner_id else None,
                'date_order': str(po.date_order) if po.date_order else None,
                'notes': po.notes or '',
            }
            pre_lines = {}
            for ol in po.order_line:
                pre_lines[ol.id] = {
                    'product_id': ol.product_id.id if ol.product_id else None,
                    'product_name': ol.product_id.display_name if ol.product_id else '',
                    'product_qty': ol.product_qty,
                    'price_unit': ol.price_unit,
                    'qty_received': ol.qty_received,
                    'qty_invoiced': ol.qty_invoiced,
                    'taxes_id': ol.taxes_id.ids,
                }

            header_vals = {}
            if partner_id:
                header_vals['partner_id'] = int(partner_id)
            if date_order:
                header_vals['date_order'] = date_order
            header_vals['notes'] = notes
            if header_vals:
                po.write(header_vals)

            # --- Collect affected picking/invoice IDs (before line changes) ---
            affected_picking_ids = set()
            affected_invoice_ids = set()
            if is_done_edit:
                for ol in po.order_line:
                    for m in ol.move_ids:
                        if m.picking_id:
                            affected_picking_ids.add(m.picking_id.id)
                    for invl in ol.invoice_lines:
                        if invl.move_id and invl.move_id.state != 'cancel':
                            affected_invoice_ids.add(invl.move_id.id)

            if lines_data:
                existing_ids = set(po.order_line.ids)
                sent_ids = set()
                line_commands = []
                modified_line_ids = set()

                for ld in lines_data:
                    product_id = ld.get('product_id')
                    if not product_id:
                        continue
                    quantity = float(ld.get('quantity', 1))
                    price_unit = float(ld.get('price_unit', 0))
                    list_price = float(ld.get('list_price', 0))
                    name = ld.get('name', '')
                    tax_ids = ld.get('tax_ids', [])

                    product = self.env['product.product'].browse(int(product_id))
                    if not name:
                        name = product.display_name

                    tax_command = [(6, 0, tax_ids)] if tax_ids else [(6, 0, product.supplier_taxes_id.ids)]

                    line_vals = {
                        'product_id': int(product_id),
                        'name': name,
                        'product_qty': quantity,
                        'price_unit': price_unit,
                        'product_uom': product.uom_po_id.id if product.uom_po_id.category_id == product.uom_id.category_id else product.uom_id.id,
                        'taxes_id': tax_command,
                        'date_planned': fields.Datetime.now(),
                    }

                    line_id = ld.get('id')
                    if line_id:
                        sent_ids.add(int(line_id))
                        modified_line_ids.add(int(line_id))
                        line_commands.append((1, int(line_id), line_vals))
                    else:
                        line_commands.append((0, 0, line_vals))

                to_remove = existing_ids - sent_ids
                for lid in to_remove:
                    modified_line_ids.discard(lid)
                    line_commands.append((2, lid))

                if line_commands:
                    po.write({'order_line': line_commands})

                for ld in lines_data:
                    product_id = ld.get('product_id')
                    if not product_id:
                        continue
                    product = self.env['product.product'].browse(int(product_id))
                    product.write({
                        'standard_price': float(ld.get('price_unit', 0)),
                        'list_price': float(ld.get('list_price', 0)),
                    })

                    po_line_id = int(ld.get('id')) if ld.get('id') else None
                    po_line = self.env['purchase.order.line'].browse(po_line_id) if po_line_id else po.order_line.filtered(
                        lambda l: l.product_id.id == int(product_id)
                    )[:1]
                    if po_line:
                        allocations_data = ld.get('location_allocations', [])
                        po_line.location_allocations.unlink()
                        for alloc in allocations_data:
                            self.env['purchase.order.line.location'].create({
                                'line_id': po_line.id,
                                'location_id': alloc['location_id'],
                                'quantity': alloc['quantity'],
                            })

            # --- Log all changes ---
            try:
                log_changes = []
                post_header = {
                    'partner_id': po.partner_id.id if po.partner_id else None,
                    'date_order': str(po.date_order) if po.date_order else None,
                    'notes': po.notes or '',
                }
                for field in ['partner_id', 'date_order', 'notes']:
                    if pre_header.get(field) != post_header.get(field):
                        log_changes.append({
                            'field_name': field,
                            'previous_value': pre_header.get(field, ''),
                            'new_value': post_header.get(field, ''),
                            'change_type': 'header',
                        })

                post_lines = {}
                for ol in po.order_line:
                    post_lines[ol.id] = {
                        'product_id': ol.product_id.id if ol.product_id else None,
                        'product_name': ol.product_id.display_name if ol.product_id else '',
                        'product_qty': ol.product_qty,
                        'price_unit': ol.price_unit,
                        'qty_received': ol.qty_received,
                        'qty_invoiced': ol.qty_invoiced,
                        'taxes_id': ol.taxes_id.ids,
                    }

                # Lines that were added
                for ol in po.order_line:
                    if ol.id not in pre_lines:
                        log_changes.append({
                            'field_name': 'order_line',
                            'previous_value': '',
                            'new_value': f"Added: {ol.product_id.display_name} qty={ol.product_qty} price={ol.price_unit}",
                            'change_type': 'line_add',
                            'line': ol,
                        })

                # Lines that were removed
                for ol_id, pre_ol in pre_lines.items():
                    if ol_id not in post_lines:
                        log_changes.append({
                            'field_name': 'order_line',
                            'previous_value': f"Removed: {pre_ol['product_name']} qty={pre_ol['product_qty']} price={pre_ol['price_unit']}",
                            'new_value': '',
                            'change_type': 'line_remove',
                        })

                # Lines that were updated
                for ol in po.order_line:
                    pre_ol = pre_lines.get(ol.id)
                    if not pre_ol:
                        continue
                    po_ol = post_lines[ol.id]
                    for fld in ['product_qty', 'price_unit']:
                        if pre_ol.get(fld) != po_ol.get(fld):
                            log_changes.append({
                                'field_name': fld,
                                'previous_value': pre_ol.get(fld, ''),
                                'new_value': po_ol.get(fld, ''),
                                'change_type': 'line_update',
                                'line': ol,
                            })
                    if pre_ol.get('product_id') != po_ol.get('product_id'):
                        log_changes.append({
                            'field_name': 'product_id',
                            'previous_value': pre_ol.get('product_name', ''),
                            'new_value': ol.product_id.display_name,
                            'change_type': 'line_update',
                            'line': ol,
                        })

                # Log state changes
                if new_state and new_state != pre_state:
                    log_changes.append({
                        'field_name': 'state',
                        'previous_value': pre_state,
                        'new_value': new_state,
                        'change_type': 'state_change',
                    })

                if log_changes:
                    self.env['purchase.order.change.log'].log_batch_changes(
                        po, log_changes,
                        picking_ids=affected_picking_ids,
                        invoice_ids=affected_invoice_ids,
                    )
            except Exception as log_e:
                _logger.error("Failed to log PO changes for %s (id=%s): %s", po.name, po.id, log_e)

            if new_state and new_state != po.state:
                if new_state == 'purchase' and po.state == 'draft':
                    _logger.info("=== UPDATE->CONFIRM PO %s (id=%s) ===", po.name, po.id)
                    for line in po.order_line:
                        prod = line.product_id
                        _logger.info(
                            "PO LINE id=%s product=%s (pid=%s) | line.product_uom=%s (cat=%s) | "
                            "product.uom_id=%s (cat=%s) | product.uom_po_id=%s (cat=%s)",
                            line.id, prod.display_name, prod.id,
                            line.product_uom.name, line.product_uom.category_id.name,
                            prod.uom_id.name, prod.uom_id.category_id.name,
                            prod.uom_po_id.name, prod.uom_po_id.category_id.name,
                        )
                    for line in po.order_line:
                        if line.product_uom.category_id != line.product_id.uom_id.category_id:
                            _logger.error(
                                "UOM MISMATCH at update-confirm: line %s product_uom=%s vs product.uom_id=%s",
                                line.id, line.product_uom.name, line.product_id.uom_id.name,
                            )
                            return {'success': False, 'message': (
                                f'لا تنتمي وحدة القياس {line.product_uom.name} المحددة في بند الطلب ({line.product_id.display_name}) '
                                f'إلى نفس الفئة التي تنتمي إليها وحدة القياس {line.product_id.uom_id.name} المحددة في المنتج. '
                                f'يرجى تصحيح وحدة القياس المحددة في بند الطلب أو في المنتج.'
                            )}
                    # Auto-fix uom_po_id to match line UOM if category differs
                    for line in po.order_line:
                        prod = line.product_id
                        if line.product_uom.category_id != prod.uom_po_id.category_id:
                            _logger.warning(
                                "AUTO-FIX uom_po_id for product %s (id=%s): %s -> %s",
                                prod.display_name, prod.id, prod.uom_po_id.name, line.product_uom.name,
                            )
                            prod.uom_po_id = line.product_uom
                    # Normalize line UOM to product purchase UOM before confirm
                    for line in po.order_line:
                        prod = line.product_id
                        if line.product_uom != prod.uom_po_id and line.product_uom.category_id == prod.uom_po_id.category_id:
                            converted_qty = line.product_uom._compute_quantity(line.product_qty, prod.uom_po_id)
                            line.write({
                                'product_qty': converted_qty,
                                'product_uom': prod.uom_po_id.id,
                            })
                    try:
                        po.button_confirm()
                    except Exception as e:
                        _logger.exception("=== UPDATE BUTTON_CONFIRM FAILED for PO %s ===", po.name)
                        raise
                elif new_state == 'cancel' and po.state != 'cancel':
                    po.button_cancel()
                elif new_state == 'draft' and po.state == 'cancel':
                    po.write({'state': 'draft'})
                elif new_state == 'sent':
                    po.write({'state': 'sent'})
                elif new_state == 'done' and po.state == 'purchase':
                    po.button_done()
                elif new_state == 'purchase' and po.state == 'done':
                    try:
                        po.button_unlock()
                    except Exception:
                        _logger.warning("Unlock failed for PO %s, user may lack manager permission", po.id)
                        return {'success': False, 'message': 'فشل إلغاء القفل. تأكد من أن لديك صلاحيات المدير.'}

            return {
                'success': True,
                'po_id': po.id,
                'name': po.name,
                'message': 'تم تحديث أمر الشراء بنجاح',
            }
        except Exception as e:
            return {'success': False, 'message': f'فشل تحديث أمر الشراء: {str(e)}'}

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
            _logger.info("=== CONFIRM PO %s (id=%s) ===", po.name, po.id)
            for line in po.order_line:
                prod = line.product_id
                _logger.info(
                    "PO LINE id=%s product=%s (pid=%s) | line.product_uom=%s (cat=%s) | "
                    "product.uom_id=%s (cat=%s) | product.uom_po_id=%s (cat=%s)",
                    line.id, prod.display_name, prod.id,
                    line.product_uom.name, line.product_uom.category_id.name,
                    prod.uom_id.name, prod.uom_id.category_id.name,
                    prod.uom_po_id.name, prod.uom_po_id.category_id.name,
                )

            # Validate UOM category compatibility before confirmation
            for line in po.order_line:
                if line.product_uom.category_id != line.product_id.uom_id.category_id:
                    _logger.error(
                        "UOM MISMATCH at confirm: line %s product_uom=%s vs product.uom_id=%s",
                        line.id, line.product_uom.name, line.product_id.uom_id.name,
                    )
                    return {'success': False, 'message': (
                        f'لا تنتمي وحدة القياس {line.product_uom.name} المحددة في بند الطلب ({line.product_id.display_name}) '
                        f'إلى نفس الفئة التي تنتمي إليها وحدة القياس {line.product_id.uom_id.name} المحددة في المنتج. '
                        f'يرجى تصحيح وحدة القياس المحددة في بند الطلب أو في المنتج.'
                    )}
            # Auto-fix uom_po_id to match line UOM if category differs
            for line in po.order_line:
                prod = line.product_id
                if line.product_uom.category_id != prod.uom_po_id.category_id:
                    _logger.warning(
                        "AUTO-FIX uom_po_id for product %s (id=%s): %s -> %s",
                        prod.display_name, prod.id, prod.uom_po_id.name, line.product_uom.name,
                    )
                    prod.uom_po_id = line.product_uom
            # Normalize line UOM to product purchase UOM before confirm
            for line in po.order_line:
                prod = line.product_id
                if line.product_uom != prod.uom_po_id and line.product_uom.category_id == prod.uom_po_id.category_id:
                    converted_qty = line.product_uom._compute_quantity(line.product_qty, prod.uom_po_id)
                    line.write({
                        'product_qty': converted_qty,
                        'product_uom': prod.uom_po_id.id,
                    })

            try:
                po.button_confirm()
            except Exception as e:
                _logger.exception("=== BUTTON_CONFIRM FAILED for PO %s ===", po.name)
                raise

            # Bypass intermediate warehouse steps for POS receipts
            for picking in po.picking_ids:
                wh = picking.picking_type_id.warehouse_id
                if wh and wh.reception_steps != 'one_step':
                    for move in picking.move_ids:
                        if move.location_dest_id == wh.wh_input_stock_loc_id:
                            move.location_dest_id = wh.lot_stock_id.id

            for line in po.order_line:
                line.product_id.write({
                    'standard_price': line.price_unit,
                    'list_price': line.product_id.list_price,
                })

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
                    move.picked = True

                    allocs_from_payload = []
                    if lines_data:
                        for ld in lines_data:
                            pid = ld.get('product_id')
                            if pid and int(pid) == move.product_id.id:
                                allocs_from_payload = ld.get('location_allocations', [])
                                break
                    line = move.purchase_line_id
                    allocs = allocs_from_payload if allocs_from_payload else (line.location_allocations if line else [])
                    if allocs:
                        single = allocs[0] if isinstance(allocs[0], dict) else allocs[:1]
                        if len(allocs) == 1:
                            loc_id = int(allocs[0].get('location_id')) if isinstance(allocs[0], dict) else allocs[0].location_id.id
                            move.location_dest_id = loc_id
                        elif len(allocs) > 1:
                            sorted_allocs = sorted(allocs, key=lambda a: a.get('quantity', 0) if isinstance(a, dict) else a.quantity, reverse=True)
                            for alloc in sorted_allocs[1:]:
                                alloc_qty = float(alloc.get('quantity', 0)) if isinstance(alloc, dict) else alloc.quantity
                                if alloc_qty <= 0:
                                    continue
                                new_move = move._split(move.product_uom_qty - alloc_qty)
                                if new_move:
                                    loc_id = int(alloc.get('location_id')) if isinstance(alloc, dict) else alloc.location_id.id
                                    new_move.location_dest_id = loc_id
                                    new_move._action_confirm()._action_assign()
                            first_loc_id = int(sorted_allocs[0].get('location_id')) if isinstance(sorted_allocs[0], dict) else sorted_allocs[0].location_id.id
                            move.location_dest_id = first_loc_id
                if picking.state != 'done':
                    wh = picking.picking_type_id.warehouse_id
                    if wh and wh.reception_steps != 'one_step':
                        for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                            if move.location_dest_id == wh.wh_input_stock_loc_id:
                                move.location_dest_id = wh.lot_stock_id.id
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
    def reverse_receive_purchase_order(self, payload):
        """Reverse receive for a purchase order — creates return stock moves,
        resets qty_received so the PO can be received again or cancelled,
        and handles linked vendor bills (draft → auto-cancel, posted → warning)."""
        po_id = payload.get('po_id')
        if not po_id:
            return {'success': False, 'message': 'معرف أمر الشراء مطلوب'}

        po = self.env['purchase.order'].browse(int(po_id))
        if not po.exists():
            return {'success': False, 'message': 'أمر الشراء غير موجود'}
        _cids = self.env.context.get('allowed_company_ids', [])
        if _cids and po.company_id.id not in _cids:
            return {'success': False, 'message': 'أمر الشراء غير موجود في هذه الشركة'}

        if po.receipt_status == 'pending':
            return {'success': False, 'message': 'لم يتم استلام أي منتجات بعد لعكسها'}

        done_pickings = po.picking_ids.filtered(lambda p: p.state == 'done')
        if not done_pickings:
            return {'success': False, 'message': 'لا توجد حركات استلام مكتملة لعكسها'}

        try:
            for picking in done_pickings:
                wizard = self.env['stock.return.picking'].create({
                    'picking_id': picking.id,
                })
                for return_line in wizard.product_return_moves:
                    return_line.quantity = return_line.move_id.quantity
                return_picking = wizard._create_return()
                if return_picking:
                    for move in return_picking.move_ids:
                        move.quantity = move.product_uom_qty
                        move.picked = True
                    return_picking.with_context(cancel_backorder=True)._action_done()

            for line in po.order_line:
                line.qty_received = 0

            # Handle linked vendor bills — draft: auto-cancel, posted: warn
            bills = po.invoice_ids.filtered(lambda b: b.state != 'cancel')
            cancelled_draft_names = []
            posted_bill_names = []

            for bill in bills:
                if bill.state == 'draft':
                    bill.button_cancel()
                    cancelled_draft_names.append(bill.display_name or f'Bill #{bill.id}')
                elif bill.state == 'posted':
                    posted_bill_names.append(bill.display_name or f'Bill #{bill.id}')

            # Force receipt_status to 'pending' — the computed field only looks
            # at picking_ids.state (all done after return), not at qty_received
            po.write({'receipt_status': False})
            po.flush_recordset(['receipt_status', 'state'])

            message = 'تم عكس استلام المنتجات بنجاح.'
            if cancelled_draft_names:
                message += f' تم إلغاء فواتير المسودة تلقائياً: ({", ".join(cancelled_draft_names)}).'

            result = {
                'success': True,
                'message': message,
                'receipt_status': 'pending',
            }
            if posted_bill_names:
                result['bill_warning'] = (
                    f'تنبيه: يوجد فواتير مرحّلة (Posted) بحاجة لمراجعة وإلغاء يدوي '
                    f'أو عمل إشعار دائن: ({", ".join(posted_bill_names)})'
                )
            return result
        except Exception as e:
            _logger.exception("فشل عكس استلام أمر الشراء %s", po_id)
            return {'success': False, 'message': f'فشل في عكس الاستلام: {str(e)}'}

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
