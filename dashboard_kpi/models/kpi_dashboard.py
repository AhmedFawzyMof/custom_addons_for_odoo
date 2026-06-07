from odoo import models, api
from datetime import datetime, timezone, timedelta


class KpiDashboard(models.Model):
    _name = 'kpi.dashboard'
    _description = 'KPI Dashboard'
    _auto = False

    def _parse_date(self, value, fallback):
        """Ensure date is valid YYYY-MM-DD string"""
        if not value:
            return fallback
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            return fallback

    @api.model
    def get_kpis(self, date_from=None, date_to=None):
        # 1. Define Cairo Timezone (UTC+2)
        cairo_tz = timezone(timedelta(hours=2))
        cairo_now = datetime.now(cairo_tz)
        today_str = cairo_now.strftime('%Y-%m-%d')
        # Default to first day of current month
        month_start_str = cairo_now.replace(day=1).strftime('%Y-%m-%d')

        # 2. Parse inputs: If empty or None, fallback to THIS MONTH
        date_from = self._parse_date(date_from, month_start_str)
        date_to = self._parse_date(date_to, today_str)

        # Build date boundaries for ORM (handles timezone automatically)
        dt_from = f"{date_from} 00:00:00"
        dt_to = f"{date_to} 23:59:59"
        d_from = date_from
        d_to = date_to

        # 1. Revenue - ORM query includes both POS orders and Sales Orders
        pos_domain = [
            ('state', 'in', ('paid', 'done', 'invoiced')),
            ('date_order', '>=', dt_from),
            ('date_order', '<=', dt_to),
        ]
        so_domain = [
            ('state', 'in', ('sale', 'done')),
            ('date_order', '>=', dt_from),
            ('date_order', '<=', dt_to),
        ]

        pos_revenue = sum(self.env['pos.order'].search(pos_domain).mapped('amount_total'))
        so_revenue = sum(self.env['sale.order'].search(so_domain).mapped('amount_total'))
        total_revenue = pos_revenue + so_revenue

        # 2. Expenses - use SQL for aggregation performance
        cr = self.env.cr
        cr.execute("""
            SELECT COALESCE(SUM(aml.debit - aml.credit), 0)
            FROM account_move_line aml
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE aml.date >= %s
              AND aml.date <= %s
              AND aa.account_type IN ('expense', 'expense_depreciation', 'expense_direct_cost')
              AND aml.parent_state = 'posted'
        """, (d_from, d_to))
        total_expenses = cr.fetchone()[0] or 0

        # 3. Low Stock
        cr.execute("""
            SELECT COUNT(*)
            FROM stock_quant sq
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
              AND sq.quantity <= 5
              AND sq.quantity > 0
        """)
        low_stock_count = cr.fetchone()[0] or 0

        # 4. Customers
        cr.execute("""
            SELECT COUNT(*)
            FROM res_partner
            WHERE customer_rank > 0
        """)
        total_customers = cr.fetchone()[0] or 0

        return {
            'date_from': date_from,
            'date_to': date_to,
            'total_revenue': float(total_revenue),
            'total_expenses': float(total_expenses),
            'low_stock_count': int(low_stock_count),
            'total_customers': int(total_customers),
        }
    
    @api.model
    def get_storage_kpi(self):
        cr = self.env.cr
        company = self.env.company
        cr.execute("""
    SELECT COALESCE(SUM(sq.quantity * COALESCE((pp.standard_price->>%(company_id)s)::double precision, 0.0)), 0)
    FROM stock_quant sq
    JOIN product_product pp ON pp.id = sq.product_id
    JOIN stock_location sl ON sl.id = sq.location_id
    WHERE sl.usage = 'internal'
      AND sq.product_id NOT IN (1, 2, 3)
""", {'company_id': str(company.id)})
        inventory_value = cr.fetchone()[0] or 0
        
        cr.execute("""
        SELECT COUNT(*)
        FROM product_product pp
        WHERE pp.id NOT IN (1, 2, 3)
          AND pp.id NOT IN (
            SELECT product_id
            FROM stock_quant sq
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal' AND sq.quantity > 0
        )
        """)
        out_of_stock = cr.fetchone()[0] or 0

        cr.execute("""
        SELECT COALESCE(SUM(sq.quantity), 0)
        FROM stock_quant sq
        JOIN stock_location sl ON sl.id = sq.location_id
        WHERE sl.usage = 'internal'
          AND sq.product_id NOT IN (1, 2, 3)
        """)
        total_quantity = cr.fetchone()[0] or 0

        cr.execute("""
        SELECT COUNT(*)
        FROM stock_picking
        WHERE state IN ('confirmed', 'assigned', 'waiting')
        AND picking_type_id IN (
            SELECT id FROM stock_picking_type WHERE code = 'incoming'
        )
        """)
        incoming_shipments = cr.fetchone()[0] or 0
        
        return {
            'inventory_value': float(inventory_value),
            'out_of_stock': int(out_of_stock),
            'total_quantity': int(total_quantity),
            'incoming_shipments': int(incoming_shipments),
        }
