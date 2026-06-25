from odoo import models, fields


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    list_price = fields.Float(
        string='Selling Price (سعر البيع)',
        digits='Product Price',
        default=0.0,
        help="Selling price set at the time of purchase. This will update the product's list_price upon PO confirmation.",
    )
