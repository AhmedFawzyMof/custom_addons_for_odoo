from odoo import models, fields


class PurchaseOrderLineLocation(models.Model):
    _name = 'purchase.order.line.location'
    _description = 'PO Line Location Allocation'

    line_id = fields.Many2one(
        'purchase.order.line',
        string='Purchase Order Line',
        required=True,
        ondelete='cascade',
    )

    location_id = fields.Many2one(
        'stock.location',
        string='Storage Location',
        required=True,
        domain=[('usage', '=', 'internal')],
    )

    quantity = fields.Float(
        string='Quantity',
        required=True,
        digits='Product Unit of Measure',
    )
