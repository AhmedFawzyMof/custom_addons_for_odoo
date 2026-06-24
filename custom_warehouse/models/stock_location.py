from odoo import fields, models

class StockLocation(models.Model):
    _inherit = 'stock.location'

    max_capacity = fields.Float(
        string="Max Capacity (units)",
        default=5000,
        help="Maximum number of units this location can hold",
    )
