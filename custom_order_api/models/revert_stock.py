import logging
from odoo import models, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class RevertStockMixin(models.AbstractModel):
    _name = 'revert.stock.mixin'
    _description = 'Stock Revert Mixin for POS Order Cancellation'

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
