from odoo import models, api, fields
from datetime import datetime, date, timezone, timedelta
from collections import defaultdict
import logging

_logger = logging.getLogger(__name__)


class PosReportsApi(models.Model):
    _name = 'pos.reports.api'
    _description = 'POS Reports API'
    _auto = False

    def _get_allowed_companies(self):
        cids = self.env.context.get('allowed_company_ids', [])
        return list(cids) if cids else list(self.env.user.company_ids.ids)

    def _parse_date(self, value, fallback):
        if not value:
            return fallback
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            return fallback

    def _table_exists(self, table_name):
        cr = self.env.cr
        cr.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = %s
            )
        """, (table_name,))
        return cr.fetchone()[0]

    def _get_dates(self, date_from, date_to):
        cairo_tz = timezone(timedelta(hours=2))
        cairo_now = datetime.now(cairo_tz)
        today_str = cairo_now.strftime('%Y-%m-%d')
        month_start_str = cairo_now.replace(day=1).strftime('%Y-%m-%d')
        date_from = self._parse_date(date_from, month_start_str)
        date_to = self._parse_date(date_to, today_str)
        return date_from, date_to, f"{date_from} 00:00:00", f"{date_to} 23:59:59"

    # ---------------------------------------------------------------
    # 1. Profit & Loss Report
    # ---------------------------------------------------------------
    @api.model
    def _report_profit_loss(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        # Revenue from POS orders
        cr.execute("""
            SELECT COALESCE(SUM(po.amount_total), 0)
            FROM pos_order po
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
        """, (dt_from, dt_to, tuple(_cids)))
        pos_revenue = float(cr.fetchone()[0])

        # Revenue from Sales Orders
        cr.execute("""
            SELECT COALESCE(SUM(so.amount_total), 0)
            FROM sale_order so
            WHERE so.state IN ('sale', 'done')
              AND so.date_order >= %s AND so.date_order <= %s
              AND so.company_id IN %s
        """, (dt_from, dt_to, tuple(_cids)))
        so_revenue = float(cr.fetchone()[0])
        total_revenue = pos_revenue + so_revenue

        # Payment totals by method type (cash / bank / pay_later)
        cr.execute("""
            SELECT CASE
                WHEN pj.type = 'cash' THEN 'cash'
                WHEN pj.type = 'bank' THEN 'bank'
                ELSE 'pay_later'
            END as payment_type,
            COALESCE(SUM(pp.amount), 0)
            FROM pos_payment pp
            JOIN pos_order po ON po.id = pp.pos_order_id
            JOIN pos_payment_method ppm ON ppm.id = pp.payment_method_id
            LEFT JOIN account_journal pj ON pj.id = ppm.journal_id
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY payment_type
        """, (dt_from, dt_to, tuple(_cids)))
        payment_totals = dict(cr.fetchall())
        cash_total = payment_totals.get('cash', 0.0)
        card_total = payment_totals.get('bank', 0.0)
        account_total = payment_totals.get('pay_later', 0.0)

        # Expenses by category
        cr.execute("""
            SELECT aa.name, COALESCE(SUM(aml.debit - aml.credit), 0) as amount
            FROM account_move_line aml
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE aml.date >= %s AND aml.date <= %s
              AND aa.account_type IN ('expense', 'expense_depreciation', 'expense_direct_cost')
              AND aml.parent_state = 'posted'
              AND aml.company_id IN %s
            GROUP BY aa.name
            ORDER BY amount DESC
        """, (d_from, d_to, tuple(_cids)))
        expense_rows = cr.fetchall()
        total_expenses = sum(r[1] for r in expense_rows) or 0.0

        # Cost of Goods Sold (safely check if svl table exists)
        cogs = 0.0
        if self._table_exists('stock_valuation_layer'):
            cr.execute("""
                SELECT COALESCE(SUM(svl.value), 0)
                FROM stock_valuation_layer svl
                JOIN stock_move sm ON sm.id = svl.stock_move_id
                WHERE sm.date >= %s AND sm.date <= %s
                  AND svl.description LIKE '%%[POS]%%'
                  AND sm.company_id IN %s
            """, (d_from, d_to, tuple(_cids)))
            cogs = float(cr.fetchone()[0])

        gross_profit = total_revenue - cogs
        net_profit = gross_profit - total_expenses

        summary = [
            {"label": "إجمالي الإيرادات", "value": f"{total_revenue:,.2f}", "icon": "trending_up", "color": "primary"},
            {"label": "تكلفة البضاعة", "value": f"{cogs:,.2f}", "icon": "shopping_cart", "color": "tertiary"},
            {"label": "إجمالي الربح", "value": f"{gross_profit:,.2f}", "icon": "account_balance_wallet", "color": "secondary"},
            {"label": "صافي الربح", "value": f"{net_profit:,.2f}", "icon": "payments", "color": "primary" if net_profit >= 0 else "error"},
            {"label": "إجمالي النقدي", "value": f"{cash_total:,.2f}", "icon": "cash", "color": "success"},
            {"label": "إجمالي البطاقة", "value": f"{card_total:,.2f}", "icon": "credit_card", "color": "primary"},
            {"label": "حساب العميل", "value": f"{account_total:,.2f}", "icon": "users", "color": "tertiary"},
        ]

        # Monthly breakdown for chart
        cr.execute("""
            SELECT TO_CHAR(po.date_order, 'YYYY-MM') as month, SUM(po.amount_total)
            FROM pos_order po
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY month ORDER BY month
        """, (dt_from, dt_to, tuple(_cids)))
        monthly_revenue = dict(cr.fetchall())

        cr.execute("""
            SELECT TO_CHAR(aml.date, 'YYYY-MM'), COALESCE(SUM(aml.debit - aml.credit), 0)
            FROM account_move_line aml
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE aml.date >= %s AND aml.date <= %s
              AND aa.account_type IN ('expense', 'expense_depreciation', 'expense_direct_cost')
              AND aml.parent_state = 'posted'
              AND aml.company_id IN %s
            GROUP BY TO_CHAR(aml.date, 'YYYY-MM') ORDER BY 1
        """, (d_from, d_to, tuple(_cids)))
        monthly_expenses = dict(cr.fetchall())

        all_months = sorted(set(list(monthly_revenue.keys()) + list(monthly_expenses.keys())))

        chart = {
            "type": "bar",
            "labels": all_months,
            "datasets": [
                {"label": "الإيرادات", "data": [float(monthly_revenue.get(m, 0)) for m in all_months]},
                {"label": "المصروفات", "data": [float(monthly_expenses.get(m, 0)) for m in all_months]},
            ],
        }

        columns = [
            {"key": "category", "label": "الفئة"},
            {"key": "amount", "label": "المبلغ", "type": "number"},
        ]
        rows = [
            {"category": "الإيرادات", "amount": total_revenue},
            {"category": "تكلفة البضاعة", "amount": cogs},
            {"category": "إجمالي الربح", "amount": gross_profit},
            {"category": "المصروفات", "amount": total_expenses},
            {"category": "صافي الربح", "amount": net_profit},
        ]

        return {"summary": summary, "chart": chart, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 2. Purchases & Sales Report
    # ---------------------------------------------------------------
    @api.model
    def _report_purchases_sales(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT TO_CHAR(po.date_order, 'YYYY-MM'), COALESCE(SUM(po.amount_total), 0)
            FROM purchase_order po
            WHERE po.state IN ('purchase', 'done')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY 1 ORDER BY 1
        """, (d_from, d_to, tuple(_cids)))
        monthly_purchases = dict(cr.fetchall())

        cr.execute("""
            SELECT TO_CHAR(po.date_order, 'YYYY-MM'), COALESCE(SUM(po.amount_total), 0)
            FROM pos_order po
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY 1 ORDER BY 1
        """, (dt_from, dt_to, tuple(_cids)))
        monthly_sales = dict(cr.fetchall())

        total_purchases = sum(monthly_purchases.values())
        total_sales = sum(monthly_sales.values())

        all_months = sorted(set(list(monthly_purchases.keys()) + list(monthly_sales.keys())))

        summary = [
            {"label": "إجمالي المشتريات", "value": f"{total_purchases:,.2f}", "icon": "truck", "color": "tertiary"},
            {"label": "إجمالي المبيعات", "value": f"{total_sales:,.2f}", "icon": "trending_up", "color": "primary"},
            {"label": "الفرق", "value": f"{total_sales - total_purchases:,.2f}", "icon": "account_balance_wallet", "color": "secondary"},
        ]

        chart = {
            "type": "bar",
            "labels": all_months,
            "datasets": [
                {"label": "المشتريات", "data": [float(monthly_purchases.get(m, 0)) for m in all_months]},
                {"label": "المبيعات", "data": [float(monthly_sales.get(m, 0)) for m in all_months]},
            ],
        }

        return {
            "summary": summary,
            "chart": chart,
            "columns": [
                {"key": "month", "label": "الشهر"},
                {"key": "purchases", "label": "المشتريات", "type": "number"},
                {"key": "sales", "label": "المبيعات", "type": "number"},
            ],
            "rows": [{"month": m, "purchases": monthly_purchases.get(m, 0), "sales": monthly_sales.get(m, 0)} for m in all_months],
        }

    # ---------------------------------------------------------------
    # 3. Tax Report
    # ---------------------------------------------------------------
    @api.model
    def _report_tax(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT at.id, at.name, COALESCE(SUM(po.amount_tax), 0)
            FROM pos_order po
            JOIN pos_order_line pol ON pol.order_id = po.id
            JOIN account_tax_pos_order_line_rel rel ON rel.pos_order_line_id = pol.id
            JOIN account_tax at ON at.id = rel.account_tax_id
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY at.id, at.name ORDER BY 3 DESC
        """, (dt_from, dt_to, tuple(_cids)))
        tax_rows = cr.fetchall()

        tax_ids = [r[0] for r in tax_rows]
        taxes = self.env['account.tax'].browse(tax_ids)
        name_map = {t.id: t.name for t in taxes}

        total_tax = sum(r[2] for r in tax_rows) or 0.0

        summary = [
            {"label": "إجمالي الضرائب", "value": f"{total_tax:,.2f}", "icon": "receipt", "color": "primary"},
            {"label": "عدد أنواع الضرائب", "value": str(len(tax_rows)), "icon": "filter", "color": "secondary"},
        ]

        chart = {
            "type": "pie",
            "labels": [name_map[r[0]] for r in tax_rows],
            "datasets": [
                {"label": "الضريبة", "data": [float(r[2]) for r in tax_rows]},
            ],
        }

        return {
            "summary": summary,
            "chart": chart,
            "columns": [
                {"key": "tax_name", "label": "نوع الضريبة"},
                {"key": "amount", "label": "المبلغ", "type": "number"},
            ],
            "rows": [{"tax_name": name_map[r[0]], "amount": float(r[2])} for r in tax_rows],
        }

    # ---------------------------------------------------------------
    # 4. Suppliers & Customers Report
    # ---------------------------------------------------------------
    @api.model
    def _report_suppliers_customers(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT COUNT(*) FROM res_partner
            WHERE supplier_rank > 0
              AND (company_id IS NULL OR company_id IN %s)
        """, (tuple(_cids),))
        total_suppliers = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT COUNT(*) FROM res_partner
            WHERE customer_rank > 0
              AND (company_id IS NULL OR company_id IN %s)
        """, (tuple(_cids),))
        total_customers = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT rp.name, COALESCE(SUM(po.amount_total), 0) as total
            FROM res_partner rp
            JOIN purchase_order po ON po.partner_id = rp.id
            WHERE rp.supplier_rank > 0
              AND po.state = 'purchase'
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY rp.name ORDER BY total DESC LIMIT 10
        """, (d_from, d_to, tuple(_cids)))
        top_suppliers = cr.fetchall()

        cr.execute("""
            SELECT rp.name, COALESCE(SUM(po.amount_total), 0) as total
            FROM res_partner rp
            JOIN pos_order po ON po.partner_id = rp.id
            WHERE rp.customer_rank > 0
              AND po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY rp.name ORDER BY total DESC LIMIT 10
        """, (dt_from, dt_to, tuple(_cids)))
        top_customers = cr.fetchall()

        summary = [
            {"label": "إجمالي الموردين", "value": str(total_suppliers), "icon": "truck", "color": "tertiary"},
            {"label": "إجمالي العملاء", "value": str(total_customers), "icon": "users", "color": "primary"},
        ]

        columns = [
            {"key": "name", "label": "الاسم"},
            {"key": "total", "label": "الإجمالي", "type": "number"},
            {"key": "type", "label": "النوع"},
        ]
        rows = (
            [{"name": r[0], "total": float(r[1]), "type": "مورد"} for r in top_suppliers] +
            [{"name": r[0], "total": float(r[1]), "type": "عميل"} for r in top_customers]
        )

        chart = {
            "type": "bar",
            "labels": ["الموردين", "العملاء"],
            "datasets": [
                {"label": "العدد", "data": [total_suppliers, total_customers]},
            ],
        }

        return {"summary": summary, "chart": chart, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 5. Customer Groups Report
    # ---------------------------------------------------------------
    @api.model
    def _report_customer_groups(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT CASE WHEN is_company THEN 'شركات (B2B)' ELSE 'أفراد (تجزئة)' END as group_name,
                   COUNT(*) as count
            FROM res_partner
            WHERE customer_rank > 0
              AND (company_id IS NULL OR company_id IN %s)
            GROUP BY is_company
        """, (tuple(_cids),))
        group_rows = cr.fetchall()

        total = sum(r[1] for r in group_rows)

        summary = [
            {"label": "إجمالي العملاء", "value": str(total), "icon": "users", "color": "primary"},
        ]

        chart = {
            "type": "pie",
            "labels": [r[0] for r in group_rows],
            "datasets": [
                {"label": "العدد", "data": [r[1] for r in group_rows]},
            ],
        }

        return {
            "summary": summary,
            "chart": chart,
            "columns": [
                {"key": "group_name", "label": "المجموعة"},
                {"key": "count", "label": "العدد", "type": "number"},
            ],
            "rows": [{"group_name": r[0], "count": r[1]} for r in group_rows],
        }

    # ---------------------------------------------------------------
    # 6. Stock Report
    # ---------------------------------------------------------------
    @api.model
    def _report_stock(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        location_id = kw.get('location_id')
        location_filter = ""
        if location_id:
            location_id = int(location_id)
            location_filter = " AND sl.id = %(location_id)s"

        cr.execute("""
            SELECT COUNT(*) FROM product_product WHERE active = True
        """)
        total_products = cr.fetchone()[0] or 0

        company = self.env.company
        params1 = {'company_id': str(company.id), 'cids': tuple(_cids)}
        if location_id:
            params1['location_id'] = location_id
        cr.execute("""
            SELECT COALESCE(SUM(sq.quantity * COALESCE((pp.standard_price->>%(company_id)s)::double precision, 0.0)), 0)
            FROM stock_quant sq
            JOIN product_product pp ON pp.id = sq.product_id
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
              AND sl.company_id IN %(cids)s
              {location_filter}
        """.format(location_filter=location_filter), params1)
        total_value = float(cr.fetchone()[0])

        params2 = {'cids': tuple(_cids)}
        if location_id:
            params2['location_id'] = location_id
        cr.execute("""
            SELECT COALESCE(SUM(sq.quantity * pt.list_price), 0)
            FROM stock_quant sq
            JOIN product_product pp ON pp.id = sq.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
              AND sl.company_id IN %(cids)s
              {location_filter}
        """.format(location_filter=location_filter), params2)
        total_sale_value = float(cr.fetchone()[0])
        potential_profit = total_sale_value - total_value

        params3 = {'cids': tuple(_cids)}
        if location_id:
            params3['location_id'] = location_id
        cr.execute("""
            SELECT COUNT(*) FROM (
                SELECT sq.product_id
                FROM stock_quant sq
                JOIN stock_location sl ON sl.id = sq.location_id
                WHERE sl.usage = 'internal'
                  AND sl.company_id IN %(cids)s
                  {location_filter}
                GROUP BY sq.product_id
                HAVING SUM(sq.quantity) <= 5 AND SUM(sq.quantity) > 0
            ) sub
        """.format(location_filter=location_filter), params3)
        low_stock = cr.fetchone()[0] or 0

        params4 = {'company_id': str(company.id), 'cids': tuple(_cids)}
        if location_id:
            params4['location_id'] = location_id
        cr.execute("""
            SELECT pt.id, pt.name, COALESCE(SUM(sq.quantity), 0) as qty,
                   COALESCE(SUM(sq.quantity * COALESCE((pp.standard_price->>%(company_id)s)::double precision, 0.0)), 0) as cost_val,
                   COALESCE(SUM(sq.quantity * pt.list_price), 0) as sale_val
            FROM stock_quant sq
            JOIN product_product pp ON pp.id = sq.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
              AND sl.company_id IN %(cids)s
              {location_filter}
            GROUP BY pt.id, pt.name
            ORDER BY cost_val DESC LIMIT 10
        """.format(location_filter=location_filter), params4)
        top_stock = cr.fetchall()

        product_tmpl_ids = [r[0] for r in top_stock]
        product_templates = self.env['product.template'].browse(product_tmpl_ids)
        product_name_map = {t.id: t.display_name for t in product_templates}

        summary = [
            {"label": "إجمالي المنتجات", "value": str(total_products), "icon": "shopping_bag", "color": "primary"},
            {"label": "قيمة المخزون (التكلفة)", "value": f"{total_value:,.2f}", "icon": "account_balance_wallet", "color": "tertiary"},
            {"label": "قيمة المخزون (سعر البيع)", "value": f"{total_sale_value:,.2f}", "icon": "trending_up", "color": "primary"},
            {"label": "الربح المحتمل", "value": f"{potential_profit:,.2f}", "icon": "payments", "color": "success" if potential_profit >= 0 else "error"},
            {"label": "منتجات منخفضة", "value": str(low_stock), "icon": "file_warning", "color": "error"},
        ]

        chart = {
            "type": "bar",
            "labels": [product_name_map[r[0]] for r in top_stock],
            "datasets": [
                {"label": "القيمة (التكلفة)", "data": [float(r[3]) for r in top_stock]},
            ],
        }

        return {
            "summary": summary,
            "chart": chart,
            "columns": [
                {"key": "product", "label": "المنتج"},
                {"key": "qty", "label": "الكمية", "type": "number"},
                {"key": "cost_value", "label": "قيمة التكلفة", "type": "number"},
                {"key": "sale_value", "label": "قيمة البيع", "type": "number"},
                {"key": "profit", "label": "الربح المحتمل", "type": "number"},
            ],
            "rows": [
                {
                    "product": product_name_map[r[0]],
                    "qty": float(r[2]),
                    "cost_value": float(r[3]),
                    "sale_value": float(r[4]),
                    "profit": float(r[4]) - float(r[3]),
                }
                for r in top_stock
            ],
        }

    # ---------------------------------------------------------------
    # 7. Damaged Stock Report
    # ---------------------------------------------------------------
    @api.model
    def _report_damaged_stock(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT ss.name, pt.id, pt.name, ss.date_done,
                   ss.state, ss.scrap_qty
            FROM stock_scrap ss
            JOIN product_product pp ON pp.id = ss.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE ss.date_done >= %s AND ss.date_done <= %s
              AND ss.company_id IN %s
            ORDER BY ss.date_done DESC
            LIMIT 200
        """, (d_from, d_to, tuple(_cids)))
        scrap_rows = cr.fetchall()

        product_tmpl_ids = list({r[1] for r in scrap_rows})
        product_templates = self.env['product.template'].browse(product_tmpl_ids)
        name_map = {t.id: t.display_name for t in product_templates}

        total_qty = sum(r[5] or 0 for r in scrap_rows)
        total_items = len(scrap_rows)

        summary = [
            {"label": "عدد مرات التلف", "value": str(total_items), "icon": "file_warning", "color": "error"},
            {"label": "إجمالي الكمية التالفة", "value": f"{total_qty:.2f}", "icon": "package", "color": "tertiary"},
        ]

        columns = [
            {"key": "reference", "label": "المرجع"},
            {"key": "product", "label": "المنتج"},
            {"key": "qty", "label": "الكمية", "type": "number"},
            {"key": "date", "label": "التاريخ"},
            {"key": "state", "label": "الحالة"},
        ]
        rows = [
            {"reference": r[0], "product": name_map[r[1]], "qty": float(r[5] or 0),
             "date": str(r[3] or ""), "state": r[4] or ""}
            for r in scrap_rows
        ]

        return {"summary": summary, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 8. Popular Products Report
    # ---------------------------------------------------------------
    @api.model
    def _report_popular_products(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT pt.id, pt.name, COALESCE(SUM(pol.qty), 0) as total_qty,
                   COALESCE(SUM(pol.price_subtotal_incl), 0) as total_revenue
            FROM pos_order_line pol
            JOIN pos_order po ON po.id = pol.order_id
            JOIN product_product pp ON pp.id = pol.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY pt.id, pt.name
            ORDER BY total_qty DESC
            LIMIT 20
        """, (dt_from, dt_to, tuple(_cids)))
        top_products = cr.fetchall()

        product_tmpl_ids = [r[0] for r in top_products]
        product_templates = self.env['product.template'].browse(product_tmpl_ids)
        product_name_map = {t.id: t.display_name for t in product_templates}

        total_qty = sum(r[2] for r in top_products)

        summary = [
            {"label": "إجمالي الكميات المباعة", "value": f"{total_qty:.0f}", "icon": "trending_up", "color": "primary"},
            {"label": "عدد المنتجات", "value": str(len(top_products)), "icon": "shopping_bag", "color": "secondary"},
        ]

        chart = {
            "type": "bar",
            "labels": [product_name_map[r[0]] for r in top_products[:10]],
            "datasets": [
                {"label": "الكمية", "data": [float(r[2]) for r in top_products[:10]]},
            ],
        }

        columns = [
            {"key": "product", "label": "المنتج"},
            {"key": "qty", "label": "الكمية", "type": "number"},
            {"key": "revenue", "label": "الإيرادات", "type": "number"},
        ]
        rows = [
            {"product": product_name_map[r[0]], "qty": float(r[2]), "revenue": float(r[3])}
            for r in top_products
        ]

        return {"summary": summary, "chart": chart, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 9. Items Report
    # ---------------------------------------------------------------
    @api.model
    def _report_items(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        cr = self.env.cr

        location_id = kw.get('location_id')
        location_filter = ""
        params = {'cids': tuple(_cids)}
        if location_id:
            location_id = int(location_id)
            location_filter = " AND sl.id = %(location_id)s"
            params['location_id'] = location_id

        cr.execute(r"""
            SELECT pt.id, pt.name, pp.default_code, pt.list_price,
                   COALESCE(sq.total_qty, 0) as qty_available,
                   (
                       SELECT STRING_AGG(pc2.name->>'en_US', ', ' ORDER BY pc2.name)
                       FROM pos_category_product_template_rel pcpt
                       JOIN pos_category pc2 ON pc2.id = pcpt.pos_category_id
                       WHERE pcpt.product_template_id = pt.id
                   ) as category
            FROM product_product pp
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN (
                SELECT product_id, SUM(quantity) as total_qty
                FROM stock_quant sq
                JOIN stock_location sl ON sl.id = sq.location_id
                WHERE sl.usage = 'internal'
                  AND sl.company_id IN %(cids)s
                  {location_filter}
                GROUP BY product_id
            ) sq ON sq.product_id = pp.id
            WHERE pp.active = True
            ORDER BY pt.name
            LIMIT 200
        """.format(location_filter=location_filter), params)
        item_rows = cr.fetchall()

        product_tmpl_ids = list({r[0] for r in item_rows})
        product_templates = self.env['product.template'].browse(product_tmpl_ids)
        name_map = {t.id: t.display_name for t in product_templates}

        total_items = len(item_rows)

        summary = [
            {"label": "إجمالي العناصر", "value": str(total_items), "icon": "shopping_bag", "color": "primary"},
        ]

        columns = [
            {"key": "code", "label": "الكود"},
            {"key": "name", "label": "الاسم"},
            {"key": "price", "label": "السعر", "type": "number"},
            {"key": "qty", "label": "المخزون", "type": "number"},
            {"key": "category", "label": "القسم"},
        ]
        rows = [
            {"code": r[2] or "", "name": name_map[r[0]], "price": float(r[3] or 0),
             "qty": float(r[4] or 0), "category": r[5] or ""}
            for r in item_rows
        ]

        return {"summary": summary, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 10. Product Purchases Report
    # ---------------------------------------------------------------
    @api.model
    def _report_product_purchases(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT pt.id, pt.name,
                   COALESCE(SUM(pol.product_qty), 0) as total_qty,
                   COALESCE(SUM(pol.price_subtotal), 0) as total_cost
            FROM purchase_order_line pol
            JOIN purchase_order po ON po.id = pol.order_id
            JOIN product_product pp ON pp.id = pol.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE po.state IN ('purchase', 'done')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY pt.id, pt.name
            ORDER BY total_cost DESC
            LIMIT 20
        """, (d_from, d_to, tuple(_cids)))
        product_rows = cr.fetchall()

        product_tmpl_ids = [r[0] for r in product_rows]
        product_templates = self.env['product.template'].browse(product_tmpl_ids)
        product_name_map = {t.id: t.display_name for t in product_templates}

        total_cost = sum(r[3] for r in product_rows)

        summary = [
            {"label": "إجمالي تكلفة المشتريات", "value": f"{total_cost:,.2f}", "icon": "truck", "color": "primary"},
            {"label": "عدد المنتجات", "value": str(len(product_rows)), "icon": "shopping_bag", "color": "secondary"},
        ]

        chart = {
            "type": "bar",
            "labels": [product_name_map[r[0]] for r in product_rows[:10]],
            "datasets": [
                {"label": "التكلفة", "data": [float(r[3]) for r in product_rows[:10]]},
            ],
        }

        columns = [
            {"key": "product", "label": "المنتج"},
            {"key": "qty", "label": "الكمية", "type": "number"},
            {"key": "cost", "label": "التكلفة", "type": "number"},
        ]
        rows = [
            {"product": product_name_map[r[0]], "qty": float(r[2]), "cost": float(r[3])}
            for r in product_rows
        ]

        return {"summary": summary, "chart": chart, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 11. Product Sales Report
    # ---------------------------------------------------------------
    @api.model
    def _report_product_sales(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT pt.id, pt.name,
                   COALESCE(SUM(pol.qty), 0) as total_qty,
                   COALESCE(SUM(pol.price_subtotal_incl), 0) as total_revenue
            FROM pos_order_line pol
            JOIN pos_order po ON po.id = pol.order_id
            JOIN product_product pp ON pp.id = pol.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY pt.id, pt.name
            ORDER BY total_qty DESC
            LIMIT 20
        """, (dt_from, dt_to, tuple(_cids)))
        product_rows = cr.fetchall()

        product_tmpl_ids = [r[0] for r in product_rows]
        product_templates = self.env['product.template'].browse(product_tmpl_ids)
        product_name_map = {t.id: t.display_name for t in product_templates}

        total_revenue = sum(r[3] for r in product_rows)

        summary = [
            {"label": "إجمالي إيرادات المبيعات", "value": f"{total_revenue:,.2f}", "icon": "trending_up", "color": "primary"},
            {"label": "عدد المنتجات", "value": str(len(product_rows)), "icon": "shopping_bag", "color": "secondary"},
        ]

        chart = {
            "type": "bar",
            "labels": [product_name_map[r[0]] for r in product_rows[:10]],
            "datasets": [
                {"label": "الإيرادات", "data": [float(r[3]) for r in product_rows[:10]]},
            ],
        }

        columns = [
            {"key": "product", "label": "المنتج"},
            {"key": "qty", "label": "الكمية", "type": "number"},
            {"key": "revenue", "label": "الإيرادات", "type": "number"},
        ]
        rows = [
            {"product": product_name_map[r[0]], "qty": float(r[2]), "revenue": float(r[3])}
            for r in product_rows
        ]

        return {"summary": summary, "chart": chart, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 12. Purchases Report
    # ---------------------------------------------------------------
    @api.model
    def _report_purchases(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT po.name, rp.name as partner, po.date_order,
                   po.amount_total, po.state
            FROM purchase_order po
            JOIN res_partner rp ON rp.id = po.partner_id
            WHERE po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            ORDER BY po.date_order DESC
            LIMIT 200
        """, (d_from, d_to, tuple(_cids)))
        purchase_rows = cr.fetchall()

        total_amount = sum(r[3] for r in purchase_rows) or 0.0
        total_pos = len(purchase_rows)

        summary = [
            {"label": "عدد أوامر الشراء", "value": str(total_pos), "icon": "clipboard_list", "color": "primary"},
            {"label": "إجمالي المبلغ", "value": f"{total_amount:,.2f}", "icon": "truck", "color": "secondary"},
        ]

        columns = [
            {"key": "name", "label": "الرقم"},
            {"key": "partner", "label": "المورد"},
            {"key": "date", "label": "التاريخ"},
            {"key": "amount", "label": "المبلغ", "type": "number"},
            {"key": "state", "label": "الحالة"},
        ]
        state_map = {"draft": "مسودة", "sent": "مرسل", "purchase": "مؤكد", "done": "مكتمل", "cancel": "ملغي"}
        rows = [
            {"name": r[0], "partner": r[1], "date": str(r[2] or ""),
             "amount": float(r[3]), "state": state_map.get(r[4], r[4])}
            for r in purchase_rows
        ]

        return {"summary": summary, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 13. Sales Report
    # ---------------------------------------------------------------
    @api.model
    def _report_sales(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        # Totals from ALL orders in date range
        cr.execute("""
            SELECT COUNT(*), COALESCE(SUM(po.amount_total), 0)
            FROM pos_order po
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
        """, (dt_from, dt_to, tuple(_cids)))
        row = cr.fetchone()
        total_orders = row[0] or 0
        total_amount = float(row[1] or 0.0)

        # Payment totals by method type
        cr.execute("""
            SELECT CASE
                WHEN pj.type = 'cash' THEN 'cash'
                WHEN pj.type = 'bank' THEN 'bank'
                ELSE 'pay_later'
            END as payment_type,
            COALESCE(SUM(pp.amount), 0)
            FROM pos_payment pp
            JOIN pos_order po ON po.id = pp.pos_order_id
            JOIN pos_payment_method ppm ON ppm.id = pp.payment_method_id
            LEFT JOIN account_journal pj ON pj.id = ppm.journal_id
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY payment_type
        """, (dt_from, dt_to, tuple(_cids)))
        payment_totals = dict(cr.fetchall())
        cash_total = payment_totals.get('cash', 0.0)
        card_total = payment_totals.get('bank', 0.0)
        account_total = payment_totals.get('pay_later', 0.0)

        # Detail rows
        cr.execute("""
            SELECT po.name, COALESCE(rp.name, 'عميل نقدي') as partner,
                   po.date_order, po.amount_total, po.amount_tax, po.state,
                    u.name as salesperson
            FROM pos_order po
            LEFT JOIN res_partner rp ON rp.id = po.partner_id
            LEFT JOIN res_users ru ON ru.id = po.user_id
            LEFT JOIN res_partner u ON u.id = ru.partner_id
            WHERE po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            ORDER BY po.date_order DESC
            LIMIT 200
        """, (dt_from, dt_to, tuple(_cids)))
        sale_rows = cr.fetchall()

        summary = [
            {"label": "عدد الطلبات", "value": str(total_orders), "icon": "receipt", "color": "primary"},
            {"label": "إجمالي المبيعات", "value": f"{total_amount:,.2f}", "icon": "trending_up", "color": "secondary"},
            {"label": "إجمالي النقدي", "value": f"{cash_total:,.2f}", "icon": "cash", "color": "success"},
            {"label": "إجمالي البطاقة", "value": f"{card_total:,.2f}", "icon": "credit_card", "color": "primary"},
            {"label": "حساب العميل", "value": f"{account_total:,.2f}", "icon": "users", "color": "tertiary"},
        ]

        columns = [
            {"key": "name", "label": "الرقم"},
            {"key": "partner", "label": "العميل"},
            {"key": "date", "label": "التاريخ"},
            {"key": "amount", "label": "الإجمالي", "type": "number"},
            {"key": "tax", "label": "الضريبة", "type": "number"},
            {"key": "salesperson", "label": "مندوب المبيعات"},
            {"key": "state", "label": "الحالة"},
        ]
        state_map = {"draft": "مسودة", "paid": "مدفوع", "done": "مكتمل", "invoiced": "محاسب", "cancel": "ملغي"}
        rows = [
            {"name": r[0], "partner": r[1], "date": str(r[2] or ""),
             "amount": float(r[3]), "tax": float(r[4] or 0),
             "salesperson": r[6] or "", "state": state_map.get(r[5], r[5])}
            for r in sale_rows
        ]

        return {"summary": summary, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 14. Expenses Report
    # ---------------------------------------------------------------
    @api.model
    def _report_expenses(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT aa.id, aa.name, COALESCE(SUM(aml.debit - aml.credit), 0) as amount
            FROM account_move_line aml
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE aml.date >= %s AND aml.date <= %s
              AND aa.account_type IN ('expense', 'expense_depreciation', 'expense_direct_cost')
              AND aml.parent_state = 'posted'
              AND aml.company_id IN %s
            GROUP BY aa.id, aa.name
            ORDER BY amount DESC
        """, (d_from, d_to, tuple(_cids)))
        expense_rows = cr.fetchall()

        account_ids = [r[0] for r in expense_rows]
        accounts = self.env['account.account'].browse(account_ids)
        name_map = {acc.id: acc.name for acc in accounts}

        total_expenses = sum(r[2] for r in expense_rows) or 0.0

        summary = [
            {"label": "إجمالي المصروفات", "value": f"{total_expenses:,.2f}", "icon": "banknote", "color": "error"},
            {"label": "عدد الفئات", "value": str(len(expense_rows)), "icon": "filter", "color": "secondary"},
        ]

        chart = {
            "type": "pie",
            "labels": [name_map[r[0]] for r in expense_rows],
            "datasets": [
                {"label": "المبلغ", "data": [float(r[2]) for r in expense_rows]},
            ],
        }

        columns = [
            {"key": "category", "label": "الفئة"},
            {"key": "amount", "label": "المبلغ", "type": "number"},
        ]
        rows = [{"category": name_map[r[0]], "amount": float(r[2])} for r in expense_rows]

        return {"summary": summary, "chart": chart, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 15. Shift Report
    # ---------------------------------------------------------------
    @api.model
    def _report_shift(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT ps.id, ps.name, pc.name as register, rp.name as user,
                   ps.start_at, ps.stop_at, ps.state,
                   (SELECT COUNT(*) FROM pos_order po WHERE po.session_id = ps.id AND po.company_id IN %s) as order_count,
                   ps.cash_register_balance_end_real
            FROM pos_session ps
            JOIN pos_config pc ON pc.id = ps.config_id
            LEFT JOIN res_users ru ON ru.id = ps.user_id
            LEFT JOIN res_partner rp ON rp.id = ru.partner_id
            WHERE ps.start_at >= %s AND (ps.stop_at <= %s OR ps.stop_at IS NULL)
              AND pc.company_id IN %s
            ORDER BY ps.start_at DESC
            LIMIT 200
        """, (tuple(_cids), dt_from, dt_to, tuple(_cids)))
        session_rows = cr.fetchall()

        total_sessions = len(session_rows)
        total_orders = sum(r[7] or 0 for r in session_rows)
        state_counts = defaultdict(int)
        for r in session_rows:
            state_counts[r[6]] += 1

        summary = [
            {"label": "إجمالي المناوبات", "value": str(total_sessions), "icon": "timer", "color": "primary"},
            {"label": "إجمالي الطلبات", "value": str(total_orders), "icon": "receipt", "color": "secondary"},
            {"label": "المناوبات المفتوحة", "value": str(state_counts.get('opened', 0) + state_counts.get('opening_control', 0)), "icon": "circle", "color": "primary"},
            {"label": "المناوبات المغلقة", "value": str(state_counts.get('closed', 0) + state_counts.get('closing_control', 0)), "icon": "circle", "color": "tertiary"},
        ]

        state_map = {"draft": "مسودة", "opening_control": "فتح", "opened": "مفتوحة", "closing_control": "غلق", "closed": "مغلقة"}
        state_colors = {"draft": "bg-gray-100", "opening_control": "bg-amber-100", "opened": "bg-emerald-100",
                        "closing_control": "bg-amber-100", "closed": "bg-slate-100"}

        columns = [
            {"key": "session_id", "label": "المعرف"},
            {"key": "name", "label": "المناوبة"},
            {"key": "register", "label": "الجهاز"},
            {"key": "user", "label": "المستخدم"},
            {"key": "start", "label": "البداية"},
            {"key": "stop", "label": "النهاية"},
            {"key": "orders", "label": "الطلبات", "type": "number"},
            {"key": "cash", "label": "النقدية", "type": "number"},
            {"key": "state", "label": "الحالة"},
        ]
        rows = [
            {"session_id": r[0], "name": r[1], "register": r[2], "user": r[3] or "",
             "start": str(r[4] or ""), "stop": str(r[5] or ""),
             "orders": r[7] or 0, "cash": float(r[8] or 0),
             "state": state_map.get(r[6], r[6]),
             "_state_color": state_colors.get(r[6], "")}
            for r in session_rows
        ]

        return {"summary": summary, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 16. Salesperson Report
    # ---------------------------------------------------------------
    @api.model
    def _report_salesperson(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT rp.name as salesperson,
                   COUNT(*) as order_count,
                   COALESCE(SUM(po.amount_total), 0) as total_sales,
                   COALESCE(AVG(po.amount_total), 0) as avg_sale
            FROM pos_order po
            JOIN res_users ru ON ru.id = po.user_id
            JOIN res_partner rp ON rp.id = ru.partner_id
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            GROUP BY rp.name
            ORDER BY total_sales DESC
        """, (dt_from, dt_to, tuple(_cids)))
        sp_rows = cr.fetchall()

        total_sales = sum(r[2] for r in sp_rows) or 0.0

        summary = [
            {"label": "عدد المندوبين", "value": str(len(sp_rows)), "icon": "users", "color": "primary"},
            {"label": "إجمالي المبيعات", "value": f"{total_sales:,.2f}", "icon": "trending_up", "color": "secondary"},
        ]

        chart = {
            "type": "bar",
            "labels": [r[0] for r in sp_rows],
            "datasets": [
                {"label": "المبيعات", "data": [float(r[2]) for r in sp_rows]},
            ],
        }

        columns = [
            {"key": "salesperson", "label": "مندوب المبيعات"},
            {"key": "orders", "label": "عدد الطلبات", "type": "number"},
            {"key": "total", "label": "إجمالي المبيعات", "type": "number"},
            {"key": "avg", "label": "متوسط الطلب", "type": "number"},
        ]
        rows = [
            {"salesperson": r[0], "orders": r[1], "total": float(r[2]), "avg": float(r[3])}
            for r in sp_rows
        ]

        return {"summary": summary, "chart": chart, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 17. Activity Log Report
    # ---------------------------------------------------------------
    @api.model
    def _report_activity_log(self, date_from, date_to, **kw):
        _cids = self._get_allowed_companies()
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        # Order activities
        cr.execute("""
            SELECT po.name, 'طلب بيع' as type, po.date_order as date,
                   rp.name as user, po.state, po.amount_total
            FROM pos_order po
            LEFT JOIN res_users ru ON ru.id = po.user_id
            LEFT JOIN res_partner rp ON rp.id = ru.partner_id
            WHERE po.date_order >= %s AND po.date_order <= %s
              AND po.company_id IN %s
            ORDER BY po.date_order DESC
            LIMIT 100
        """, (dt_from, dt_to, tuple(_cids)))
        order_activities = cr.fetchall()

        # Stock move activities
        cr.execute("""
            SELECT sml.reference, 'حركة مخزون' as type, sml.date as date,
                   rp.name as user, sml.state, sml.quantity
            FROM stock_move_line sml
            LEFT JOIN res_users ru ON ru.id = sml.create_uid
            LEFT JOIN res_partner rp ON rp.id = ru.partner_id
            WHERE sml.date >= %s AND sml.date <= %s
              AND sml.company_id IN %s
            ORDER BY sml.date DESC
            LIMIT 100
        """, (dt_from, dt_to, tuple(_cids)))
        stock_activities = cr.fetchall()

        all_activities = order_activities + stock_activities
        all_activities.sort(key=lambda x: str(x[2] or ""), reverse=True)

        total_activities = len(all_activities)

        summary = [
            {"label": "إجمالي النشاطات", "value": str(total_activities), "icon": "history", "color": "primary"},
        ]

        columns = [
            {"key": "reference", "label": "المرجع"},
            {"key": "type", "label": "النوع"},
            {"key": "date", "label": "التاريخ"},
            {"key": "user", "label": "المستخدم"},
            {"key": "detail", "label": "التفاصيل"},
        ]
        type_colors = {"طلب بيع": "bg-primary/10 text-primary", "حركة مخزون": "bg-amber-500/10 text-amber-600"}
        rows = [
            {"reference": r[0], "type": r[1], "date": str(r[2] or ""),
             "user": r[3] or "", "detail": f"{r[4]} | {float(r[5] or 0):.2f}",
             "_type_color": type_colors.get(r[1], "")}
            for r in all_activities
        ]

        return {"summary": summary, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 18. Late Payments Report
    # ---------------------------------------------------------------
    @api.model
    def _report_late_payments(self, date_from, date_to, **kw):
        today = date.today()
        _cids = self._get_allowed_companies()
        cr = self.env.cr

        company_domain = [('company_id', 'in', tuple(_cids))] if _cids else []

        # Overdue vendor bills
        bill_domain = [
            ('move_type', '=', 'in_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', '!=', 'paid'),
            ('invoice_date_due', '<', today),
        ] + company_domain
        bills = self.env['account.move'].search(bill_domain, order='invoice_date_due asc', limit=200)

        bill_items = []
        for b in bills:
            days_overdue = (today - b.invoice_date_due).days if b.invoice_date_due else 0
            bucket = '90+'
            if days_overdue <= 30:
                bucket = '0-30'
            elif days_overdue <= 60:
                bucket = '30-60'
            elif days_overdue <= 90:
                bucket = '60-90'
            bill_items.append({
                'bucket': bucket,
                'amount': b.amount_residual,
                'reference': b.name,
                'partner': b.partner_id.name if b.partner_id else '',
                'date_due': b.invoice_date_due.strftime('%Y-%m-%d') if b.invoice_date_due else '',
                'days': days_overdue,
            })

        # Overdue purchase orders (confirmed but not fully received)
        po_domain = [
            ('state', '=', 'purchase'),
            ('receipt_status', 'in', ('pending', 'partial')),
        ] + company_domain
        pos = self.env['purchase.order'].search(po_domain, order='date_order asc', limit=200)

        po_items = []
        for po in pos:
            date_planned = po.order_line and min(
                (l.date_planned.date() for l in po.order_line if l.date_planned),
                default=None
            )
            days_overdue = 0
            if date_planned and date_planned < today:
                days_overdue = (today - date_planned).days
            elif not date_planned and po.date_order:
                po_date = po.date_order.date() if hasattr(po.date_order, 'date') else po.date_order
                if po_date < today:
                    days_overdue = (today - po_date).days
            if days_overdue <= 0:
                continue

            bucket = '90+'
            if days_overdue <= 30:
                bucket = '0-30'
            elif days_overdue <= 60:
                bucket = '30-60'
            elif days_overdue <= 90:
                bucket = '60-90'

            po_items.append({
                'bucket': bucket,
                'amount': po.amount_total,
                'reference': po.name,
                'partner': po.partner_id.name if po.partner_id else '',
                'date_due': date_planned.strftime('%Y-%m-%d') if date_planned else '',
                'days': days_overdue,
            })

        all_items = bill_items + po_items

        # Summary cards
        total_overdue = len(all_items)
        total_amount = sum(i['amount'] for i in all_items)
        buckets = defaultdict(int)
        bucket_amounts = defaultdict(float)
        for i in all_items:
            buckets[i['bucket']] += 1
            bucket_amounts[i['bucket']] += i['amount']

        summary = [
            {"label": "إجمالي المتأخرات", "value": str(total_overdue), "icon": "file_warning", "color": "error"},
            {"label": "القيمة المتأخرة", "value": f"{total_amount:,.2f}", "icon": "banknote", "color": "error"},
            {"label": "فواتير متأخرة", "value": str(len(bill_items)), "icon": "receipt", "color": "primary"},
            {"label": "أوامر شراء متأخرة", "value": str(len(po_items)), "icon": "clipboard_list", "color": "tertiary"},
        ]

        # Chart: aging buckets bar
        chart = {
            "type": "bar",
            "labels": ['0-30 يوم', '30-60 يوم', '60-90 يوم', 'أكثر من 90 يوم'],
            "datasets": [
                {"label": "العدد", "data": [buckets.get('0-30', 0), buckets.get('30-60', 0), buckets.get('60-90', 0), buckets.get('90+', 0)]},
            ],
        }

        columns = [
            {"key": "type", "label": "النوع"},
            {"key": "reference", "label": "المرجع"},
            {"key": "partner", "label": "المورد"},
            {"key": "date_due", "label": "تاريخ الاستحقاق"},
            {"key": "days", "label": "أيام التأخير", "type": "number"},
            {"key": "bucket", "label": "الفترة"},
            {"key": "amount", "label": "المبلغ", "type": "number"},
        ]
        rows = (
            [{"type": "فاتورة مورد", "reference": i['reference'], "partner": i['partner'],
              "date_due": i['date_due'], "days": i['days'], "bucket": i['bucket'],
              "amount": float(i['amount'])} for i in bill_items] +
            [{"type": "أمر شراء", "reference": i['reference'], "partner": i['partner'],
              "date_due": i['date_due'], "days": i['days'], "bucket": i['bucket'],
              "amount": float(i['amount'])} for i in po_items]
        )

        return {"summary": summary, "chart": chart, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # Open Sessions (for close-session dropdown)
    # ---------------------------------------------------------------
    @api.model
    def _get_open_sessions(self, date_from=None, date_to=None, **kw):
        _cids = self._get_allowed_companies()
        try:
            states = kw.get('states', 'opened,closing_control').split(',')
            domain = [
                ('state', 'in', states),
                ('config_id.company_id', 'in', _cids),
            ]
            sessions = self.env['pos.session'].search(domain, order='name desc')
            result = [{
                "id": s.id,
                "name": s.name,
                "config": s.config_id.name,
                "user": s.user_id.partner_id.name or s.user_id.name or "",
                "state": s.state,
                "start_at": str(s.start_at or ""),
                "order_count": len(s.order_ids),
                "total_sales": sum(s.order_ids.filtered(lambda o: o.state not in ('draft', 'cancel')).mapped('amount_total')),
            } for s in sessions]
            _logger.info(f"_get_open_sessions: found {len(result)} sessions for companies {_cids}")
            return {"data": result}
        except Exception as e:
            _logger.error(f"_get_open_sessions error: {e}", exc_info=True)
            return {"data": [], "error": str(e)}

    # ---------------------------------------------------------------
    # Close POS Session
    # ---------------------------------------------------------------
    @api.model
    def _close_pos_session(self, date_from=None, date_to=None, **kw):
        session_id = kw.get('session_id')
        if not session_id:
            return {"status": "error", "message": "معرف الجلسة مطلوب"}

        if not self.env.user.has_group('point_of_sale.group_pos_manager'):
            return {"status": "error", "message": "ليس لديك صلاحية إغلاق الجلسات"}

        session = self.env['pos.session'].browse(int(session_id))
        if not session.exists():
            return {"status": "error", "message": "الجلسة غير موجودة"}
        if session.state not in ('opened', 'closing_control'):
            return {"status": "error", "message": "الجلسة ليست مفتوحة أو في طور الإغلاق"}

        try:
            session_sudo = session.sudo().with_company(session.company_id)
            if session_sudo.state == 'opened':
                session_sudo.write({'state': 'closing_control', 'stop_at': fields.Datetime.now()})
                self.env.cr.flush()
            if session_sudo.state == 'closing_control':
                result = session_sudo._validate_session()
                if isinstance(result, dict):
                    return {"status": "error", "message": result.get('name', 'فشل التسوية المحاسبية')}
            return {"status": "success", "message": f"تم إغلاق الجلسة {session.name} بنجاح"}
        except Exception as e:
            _logger.error(f"POS close session error: {e}", exc_info=True)
            return {"status": "error", "message": f"فشل إغلاق الجلسة: {str(e)}"}

    # ---------------------------------------------------------------
    # Main entry point
    # ---------------------------------------------------------------
    @api.model
    def get_report_data(self, report_type, date_from=None, date_to=None, filters=None):
        method_map = {
            "profit_loss": "_report_profit_loss",
            "purchases_sales": "_report_purchases_sales",
            "tax": "_report_tax",
            "suppliers_customers": "_report_suppliers_customers",
            "customer_groups": "_report_customer_groups",
            "stock": "_report_stock",
            "damaged_stock": "_report_damaged_stock",
            "popular_products": "_report_popular_products",
            "items": "_report_items",
            "product_purchases": "_report_product_purchases",
            "product_sales": "_report_product_sales",
            "purchases": "_report_purchases",
            "sales": "_report_sales",
            "expenses": "_report_expenses",
            "shift": "_report_shift",
            "salesperson": "_report_salesperson",
            "activity_log": "_report_activity_log",
            "late_payments": "_report_late_payments",
            "open_sessions": "_get_open_sessions",
            "close_session": "_close_pos_session",
        }
        method_name = method_map.get(report_type)
        if not method_name:
            return {"error": f"Unknown report type: {report_type}"}
        method = getattr(self, method_name)
        return method(date_from, date_to, **filters)
