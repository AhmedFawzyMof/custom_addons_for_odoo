from odoo import models, api, fields
from datetime import datetime, timezone, timedelta
from collections import defaultdict


class PosReportsApi(models.Model):
    _name = 'pos.reports.api'
    _description = 'POS Reports API'
    _auto = False

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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        # Revenue from POS orders
        cr.execute("""
            SELECT COALESCE(SUM(po.amount_total), 0)
            FROM pos_order po
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
        """, (dt_from, dt_to))
        pos_revenue = float(cr.fetchone()[0])

        # Revenue from Sales Orders
        cr.execute("""
            SELECT COALESCE(SUM(so.amount_total), 0)
            FROM sale_order so
            WHERE so.state IN ('sale', 'done')
              AND so.date_order >= %s AND so.date_order <= %s
        """, (dt_from, dt_to))
        so_revenue = float(cr.fetchone()[0])
        total_revenue = pos_revenue + so_revenue

        # Expenses by category
        cr.execute("""
            SELECT aa.name, COALESCE(SUM(aml.debit - aml.credit), 0) as amount
            FROM account_move_line aml
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE aml.date >= %s AND aml.date <= %s
              AND aa.account_type IN ('expense', 'expense_depreciation', 'expense_direct_cost')
              AND aml.parent_state = 'posted'
            GROUP BY aa.name
            ORDER BY amount DESC
        """, (d_from, d_to))
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
            """, (d_from, d_to))
            cogs = float(cr.fetchone()[0])

        gross_profit = total_revenue - cogs
        net_profit = gross_profit - total_expenses

        summary = [
            {"label": "إجمالي الإيرادات", "value": f"{total_revenue:,.2f}", "icon": "trending_up", "color": "primary"},
            {"label": "تكلفة البضاعة", "value": f"{cogs:,.2f}", "icon": "shopping_cart", "color": "tertiary"},
            {"label": "إجمالي الربح", "value": f"{gross_profit:,.2f}", "icon": "account_balance_wallet", "color": "secondary"},
            {"label": "صافي الربح", "value": f"{net_profit:,.2f}", "icon": "payments", "color": "primary" if net_profit >= 0 else "error"},
        ]

        # Monthly breakdown for chart
        cr.execute("""
            SELECT TO_CHAR(po.date_order, 'YYYY-MM') as month, SUM(po.amount_total)
            FROM pos_order po
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
            GROUP BY month ORDER BY month
        """, (dt_from, dt_to))
        monthly_revenue = dict(cr.fetchall())

        cr.execute("""
            SELECT TO_CHAR(aml.date, 'YYYY-MM'), COALESCE(SUM(aml.debit - aml.credit), 0)
            FROM account_move_line aml
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE aml.date >= %s AND aml.date <= %s
              AND aa.account_type IN ('expense', 'expense_depreciation', 'expense_direct_cost')
              AND aml.parent_state = 'posted'
            GROUP BY TO_CHAR(aml.date, 'YYYY-MM') ORDER BY 1
        """, (d_from, d_to))
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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT TO_CHAR(po.date_order, 'YYYY-MM'), COALESCE(SUM(po.amount_total), 0)
            FROM purchase_order po
            WHERE po.state IN ('purchase', 'done')
              AND po.date_order >= %s AND po.date_order <= %s
            GROUP BY 1 ORDER BY 1
        """, (d_from, d_to))
        monthly_purchases = dict(cr.fetchall())

        cr.execute("""
            SELECT TO_CHAR(po.date_order, 'YYYY-MM'), COALESCE(SUM(po.amount_total), 0)
            FROM pos_order po
            WHERE po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
            GROUP BY 1 ORDER BY 1
        """, (dt_from, dt_to))
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
            GROUP BY at.id, at.name ORDER BY 3 DESC
        """, (dt_from, dt_to))
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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT COUNT(*) FROM res_partner WHERE supplier_rank > 0
        """)
        total_suppliers = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT COUNT(*) FROM res_partner WHERE customer_rank > 0
        """)
        total_customers = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT rp.name, COALESCE(SUM(po.amount_total), 0) as total
            FROM res_partner rp
            JOIN purchase_order po ON po.partner_id = rp.id
            WHERE rp.supplier_rank > 0
              AND po.state = 'purchase'
              AND po.date_order >= %s AND po.date_order <= %s
            GROUP BY rp.name ORDER BY total DESC LIMIT 10
        """, (d_from, d_to))
        top_suppliers = cr.fetchall()

        cr.execute("""
            SELECT rp.name, COALESCE(SUM(po.amount_total), 0) as total
            FROM res_partner rp
            JOIN pos_order po ON po.partner_id = rp.id
            WHERE rp.customer_rank > 0
              AND po.state IN ('paid', 'done', 'invoiced')
              AND po.date_order >= %s AND po.date_order <= %s
            GROUP BY rp.name ORDER BY total DESC LIMIT 10
        """, (dt_from, dt_to))
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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT CASE WHEN is_company THEN 'شركات (B2B)' ELSE 'أفراد (تجزئة)' END as group_name,
                   COUNT(*) as count
            FROM res_partner
            WHERE customer_rank > 0
            GROUP BY is_company
        """)
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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT COUNT(*) FROM product_product WHERE active = True
        """)
        total_products = cr.fetchone()[0] or 0

        company = self.env.company
        cr.execute("""
            SELECT COALESCE(SUM(sq.quantity * COALESCE((pp.standard_price->>%(company_id)s)::double precision, 0.0)), 0)
            FROM stock_quant sq
            JOIN product_product pp ON pp.id = sq.product_id
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
        """, {'company_id': str(company.id)})
        total_value = float(cr.fetchone()[0])

        cr.execute("""
            SELECT COUNT(*) FROM (
                SELECT sq.product_id
                FROM stock_quant sq
                JOIN stock_location sl ON sl.id = sq.location_id
                WHERE sl.usage = 'internal'
                GROUP BY sq.product_id
                HAVING SUM(sq.quantity) <= 5 AND SUM(sq.quantity) > 0
            ) sub
        """)
        low_stock = cr.fetchone()[0] or 0

        cr.execute("""
            SELECT pt.id, pt.name, COALESCE(SUM(sq.quantity), 0) as qty,
                   COALESCE(SUM(sq.quantity * COALESCE((pp.standard_price->>%(company_id)s)::double precision, 0.0)), 0) as val
            FROM stock_quant sq
            JOIN product_product pp ON pp.id = sq.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN stock_location sl ON sl.id = sq.location_id
            WHERE sl.usage = 'internal'
            GROUP BY pt.id, pt.name
            ORDER BY val DESC LIMIT 10
        """, {'company_id': str(company.id)})
        top_stock = cr.fetchall()

        product_tmpl_ids = [r[0] for r in top_stock]
        product_templates = self.env['product.template'].browse(product_tmpl_ids)
        product_name_map = {t.id: t.display_name for t in product_templates}

        summary = [
            {"label": "إجمالي المنتجات", "value": str(total_products), "icon": "shopping_bag", "color": "primary"},
            {"label": "قيمة المخزون", "value": f"{total_value:,.2f}", "icon": "account_balance_wallet", "color": "secondary"},
            {"label": "منتجات منخفضة", "value": str(low_stock), "icon": "file_warning", "color": "error"},
        ]

        chart = {
            "type": "bar",
            "labels": [product_name_map[r[0]] for r in top_stock],
            "datasets": [
                {"label": "القيمة", "data": [float(r[3]) for r in top_stock]},
            ],
        }

        return {
            "summary": summary,
            "chart": chart,
            "columns": [
                {"key": "product", "label": "المنتج"},
                {"key": "qty", "label": "الكمية", "type": "number"},
                {"key": "value", "label": "القيمة", "type": "number"},
            ],
            "rows": [{"product": product_name_map[r[0]], "qty": float(r[2]), "value": float(r[3])} for r in top_stock],
        }

    # ---------------------------------------------------------------
    # 7. Damaged Stock Report
    # ---------------------------------------------------------------
    @api.model
    def _report_damaged_stock(self, date_from, date_to, **kw):
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT ss.name, pt.id, pt.name, ss.date_done,
                   ss.state, ss.scrap_qty
            FROM stock_scrap ss
            JOIN product_product pp ON pp.id = ss.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE ss.date_done >= %s AND ss.date_done <= %s
            ORDER BY ss.date_done DESC
        """, (d_from, d_to))
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
            GROUP BY pt.id, pt.name
            ORDER BY total_qty DESC
            LIMIT 20
        """, (dt_from, dt_to))
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
        cr = self.env.cr

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
                GROUP BY product_id
            ) sq ON sq.product_id = pp.id
            WHERE pp.active = True
            ORDER BY pt.name
            LIMIT 200
        """)
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
            GROUP BY pt.id, pt.name
            ORDER BY total_cost DESC
            LIMIT 20
        """, (d_from, d_to))
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
            GROUP BY pt.id, pt.name
            ORDER BY total_qty DESC
            LIMIT 20
        """, (dt_from, dt_to))
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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT po.name, rp.name as partner, po.date_order,
                   po.amount_total, po.state
            FROM purchase_order po
            JOIN res_partner rp ON rp.id = po.partner_id
            WHERE po.date_order >= %s AND po.date_order <= %s
            ORDER BY po.date_order DESC
            LIMIT 200
        """, (d_from, d_to))
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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT po.name, COALESCE(rp.name, 'عميل نقدي') as partner,
                   po.date_order, po.amount_total, po.amount_tax, po.state,
                    u.name as salesperson
            FROM pos_order po
            LEFT JOIN res_partner rp ON rp.id = po.partner_id
            LEFT JOIN res_users ru ON ru.id = po.user_id
            LEFT JOIN res_partner u ON u.id = ru.partner_id
            WHERE po.date_order >= %s AND po.date_order <= %s
            ORDER BY po.date_order DESC
            LIMIT 200
        """, (dt_from, dt_to))
        sale_rows = cr.fetchall()

        total_amount = sum(r[3] for r in sale_rows) or 0.0
        total_orders = len(sale_rows)

        summary = [
            {"label": "عدد الطلبات", "value": str(total_orders), "icon": "receipt", "color": "primary"},
            {"label": "إجمالي المبيعات", "value": f"{total_amount:,.2f}", "icon": "trending_up", "color": "secondary"},
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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT aa.id, aa.name, COALESCE(SUM(aml.debit - aml.credit), 0) as amount
            FROM account_move_line aml
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE aml.date >= %s AND aml.date <= %s
              AND aa.account_type IN ('expense', 'expense_depreciation', 'expense_direct_cost')
              AND aml.parent_state = 'posted'
            GROUP BY aa.id, aa.name
            ORDER BY amount DESC
        """, (d_from, d_to))
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
        d_from, d_to, dt_from, dt_to = self._get_dates(date_from, date_to)
        cr = self.env.cr

        cr.execute("""
            SELECT ps.name, pc.name as register, rp.name as user,
                   ps.start_at, ps.stop_at, ps.state,
                   (SELECT COUNT(*) FROM pos_order po WHERE po.session_id = ps.id) as order_count,
                   ps.cash_register_balance_end_real
            FROM pos_session ps
            JOIN pos_config pc ON pc.id = ps.config_id
            LEFT JOIN res_users ru ON ru.id = ps.user_id
            LEFT JOIN res_partner rp ON rp.id = ru.partner_id
            WHERE ps.start_at >= %s AND (ps.stop_at <= %s OR ps.stop_at IS NULL)
            ORDER BY ps.start_at DESC
            LIMIT 200
        """, (dt_from, dt_to))
        session_rows = cr.fetchall()

        total_sessions = len(session_rows)
        total_orders = sum(r[6] or 0 for r in session_rows)
        state_counts = defaultdict(int)
        for r in session_rows:
            state_counts[r[5]] += 1

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
            {"name": r[0], "register": r[1], "user": r[2] or "",
             "start": str(r[3] or ""), "stop": str(r[4] or ""),
             "orders": r[6] or 0, "cash": float(r[7] or 0),
             "state": state_map.get(r[5], r[5]),
             "_state_color": state_colors.get(r[5], "")}
            for r in session_rows
        ]

        return {"summary": summary, "columns": columns, "rows": rows}

    # ---------------------------------------------------------------
    # 16. Salesperson Report
    # ---------------------------------------------------------------
    @api.model
    def _report_salesperson(self, date_from, date_to, **kw):
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
            GROUP BY rp.name
            ORDER BY total_sales DESC
        """, (dt_from, dt_to))
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
            ORDER BY po.date_order DESC
            LIMIT 100
        """, (dt_from, dt_to))
        order_activities = cr.fetchall()

        # Stock move activities
        cr.execute("""
            SELECT sml.reference, 'حركة مخزون' as type, sml.date as date,
                   rp.name as user, sml.state, sml.quantity
            FROM stock_move_line sml
            LEFT JOIN res_users ru ON ru.id = sml.create_uid
            LEFT JOIN res_partner rp ON rp.id = ru.partner_id
            WHERE sml.date >= %s AND sml.date <= %s
            ORDER BY sml.date DESC
            LIMIT 100
        """, (dt_from, dt_to))
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
        }
        method_name = method_map.get(report_type)
        if not method_name:
            return {"error": f"Unknown report type: {report_type}"}
        method = getattr(self, method_name)
        return method(date_from, date_to, **filters)
