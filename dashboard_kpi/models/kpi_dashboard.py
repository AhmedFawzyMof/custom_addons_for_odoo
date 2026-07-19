import logging
from datetime import datetime, timezone, timedelta
from odoo import models, api

_logger = logging.getLogger(__name__)


class KpiDashboard(models.Model):
    _name = 'kpi.dashboard'
    _description = 'KPI Dashboard'
    _auto = False

    def _get_allowed_companies(self):
        cids = self.env.context.get('allowed_company_ids', [])
        return cids or self.env.user.company_ids.ids

    def _parse_date(self, value, fallback):
        if not value:
            return fallback
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            return fallback

    @api.model
    def get_kpis(self, date_from=None, date_to=None):
        _logger.info(
            "KPI START | date_from=%s | date_to=%s | timestamp=%s",
            date_from, date_to, datetime.utcnow().isoformat(),
        )
        cairo_tz = timezone(timedelta(hours=2))
        cairo_now = datetime.now(cairo_tz)
        today_str = cairo_now.strftime('%Y-%m-%d')
        now_str = cairo_now.strftime('%Y-%m-%d %H:%M:%S')

        date_from = self._parse_date(date_from, today_str)
        date_to = self._parse_date(date_to, today_str)

        dt_from = f"{date_from} 00:00:00"
        if date_from == today_str and date_to == today_str:
            dt_to = now_str
        else:
            dt_to = f"{date_to} 23:59:59"
        d_from = date_from
        d_to = date_to

        self.env.cr.commit()  # ensure latest data

        _company_ids = self._get_allowed_companies()
        _logger.info("KPI PARAMS | date_from=%s | date_to=%s | dt_from=%s | dt_to=%s | companies=%s", date_from, date_to, dt_from, dt_to, _company_ids)

        # Revenue via SQL to avoid ORM cache
        cr = self.env.cr
        cr.execute("""
            SELECT COALESCE(SUM(amount_total), 0) FROM pos_order
            WHERE state IN ('paid', 'done', 'invoiced')
              AND date_order >= %s AND date_order <= %s
              AND company_id IN %s
        """, (dt_from, dt_to, tuple(_company_ids)))
        pos_revenue = float(cr.fetchone()[0])

        cr.execute("""
            SELECT COALESCE(SUM(amount_total), 0) FROM sale_order
            WHERE state IN ('sale', 'done')
              AND date_order >= %s AND date_order <= %s
              AND company_id IN %s
        """, (dt_from, dt_to, tuple(_company_ids)))
        so_revenue = float(cr.fetchone()[0])
        total_revenue = pos_revenue + so_revenue

        cr.execute("""
            SELECT COALESCE(SUM(aml.debit - aml.credit), 0)
            FROM account_move_line aml
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE aml.date >= %s
              AND aml.date <= %s
              AND aa.account_type IN ('expense', 'expense_depreciation', 'expense_direct_cost')
              AND aml.parent_state = 'posted'
              AND aml.company_id IN %s
        """, (d_from, d_to, tuple(_company_ids)))
        total_expenses = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT COUNT(*)
            FROM stock_quant sq
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
              AND sq.quantity <= 5
              AND sq.quantity > 0
              AND sl.company_id IN %s
        """, (tuple(_company_ids),))
        low_stock_count = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT COUNT(*)
            FROM res_partner
            WHERE customer_rank > 0
              AND (company_id IS NULL OR company_id IN %s)
        """, (tuple(_company_ids),))
        total_customers = cr.fetchone()[0] or 0

        result = {
            'date_from': date_from,
            'date_to': date_to,
            'total_revenue': float(total_revenue),
            'total_expenses': float(total_expenses),
            'low_stock_count': int(low_stock_count),
            'total_customers': int(total_customers),
        }

        _logger.info(
            "KPI RESULT | revenue=%s | expenses=%s | low_stock=%s | customers=%s",
            total_revenue, total_expenses, low_stock_count, total_customers,
        )
        return result
    
    @api.model
    def get_storage_kpi(self):
        _logger.info("KPI STORAGE START | timestamp=%s", datetime.utcnow().isoformat())
        _company_ids = self._get_allowed_companies()
        cr = self.env.cr
        self.env.cr.commit()
        company = self.env.company

        cr.execute("""
            SELECT COALESCE(SUM(sq.quantity * COALESCE((pp.standard_price->>%(company_id)s)::double precision, 0.0)), 0)
            FROM stock_quant sq
            JOIN product_product pp ON pp.id = sq.product_id
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
              AND sq.product_id NOT IN (1, 2, 3)
              AND sl.company_id IN %(company_ids)s
        """, {'company_id': str(company.id), 'company_ids': tuple(_company_ids)})
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
                  AND sl.company_id IN %s
            )
        """, (tuple(_company_ids),))
        out_of_stock = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT COALESCE(SUM(sq.quantity), 0)
            FROM stock_quant sq
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
              AND sq.product_id NOT IN (1, 2, 3)
              AND sl.company_id IN %s
        """, (tuple(_company_ids),))
        total_quantity = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT COUNT(*)
            FROM stock_picking
            WHERE state IN ('confirmed', 'assigned', 'waiting')
            AND picking_type_id IN (
                SELECT id FROM stock_picking_type WHERE code = 'incoming'
            )
            AND company_id IN %s
        """, (tuple(_company_ids),))
        incoming_shipments = cr.fetchone()[0] or 0

        result = {
            'inventory_value': float(inventory_value),
            'out_of_stock': int(out_of_stock),
            'total_quantity': int(total_quantity),
            'incoming_shipments': int(incoming_shipments),
        }

        _logger.info(
            "KPI STORAGE RESULT | inventory_value=%s | out_of_stock=%s | total_qty=%s | incoming=%s",
            inventory_value, out_of_stock, total_quantity, incoming_shipments,
        )
        return result
