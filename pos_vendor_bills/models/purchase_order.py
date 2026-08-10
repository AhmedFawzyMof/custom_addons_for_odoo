from odoo import models


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    def _sync_product_prices_from_lines(self):
        """Propagate buying (standard_price) and selling (list_price) prices
        from PO lines to the ordered products.

        - Buying price is taken from the line unit price, converted to the
          product's default UOM (the unit price entered on the line is
          expressed in the line's UOM).
        - Selling price is the line's `list_price` (the selling price recorded
          at purchase time). When it is not set (0.0), the product's current
          selling price is left untouched so existing behaviour is preserved.
        - A product appearing on several lines is written exactly once (the
          last line wins) so repeated lines cannot double-write.
        - Only products actually present on the order lines are touched;
          unrelated products are never modified.
        """
        prices_by_product = {}
        for order in self:
            for line in order.order_line.filtered(lambda l: l.product_id):
                product = line.product_id
                if line.product_uom.category_id == product.uom_id.category_id:
                    cost = line.product_uom._compute_price(line.price_unit, product.uom_id)
                else:
                    cost = line.price_unit
                prices_by_product[product.id] = {
                    'standard_price': cost,
                    'list_price': line.list_price,
                }
        for order in self:
            for product in order.order_line.product_id:
                prices = prices_by_product[product.id]
                vals = {'standard_price': prices['standard_price']}
                if prices['list_price'] > 0:
                    vals['list_price'] = prices['list_price']
                product.with_company(order.company_id).write(vals)
        return True

    def button_approve(self, force=False):
        res = super().button_approve(force=force)
        for order in self.filtered(lambda o: o.state == 'purchase'):
            order._sync_product_prices_from_lines()
        return res