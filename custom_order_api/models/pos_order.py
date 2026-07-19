from odoo import fields, models


class PosOrder(models.Model):
    _inherit = 'pos.order'

    order_discount = fields.Float(string='Order Discount', default=0.0)
    order_discount_type = fields.Selection([
        ('fixed', 'Fixed'),
        ('percent', 'Percentage'),
    ], string='Discount Type', default='fixed')
    service_fee = fields.Float(string='Service Fee', default=0.0)
    service_fee_type = fields.Selection([
        ('fixed', 'Fixed'),
        ('percent', 'Percentage'),
    ], string='Service Fee Type', default='fixed')
