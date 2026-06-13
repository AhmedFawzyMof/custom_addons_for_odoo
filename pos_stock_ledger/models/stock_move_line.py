import math
from odoo import models, fields, api

class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    def _get_company_domain(self):
        cids = self.env.context.get('allowed_company_ids', [])
        if cids:
            return [('company_id', 'in', cids)]
        return []

    @api.model
    def get_frontend_ledger(self, params=None):
        if params is None:
            params = {}

        # 1. Parse Pagination
        try:
            page = max(1, int(params.get('page', 1)))
            limit = max(1, int(params.get('limit', 8)))
        except (ValueError, TypeError):
            page = 1
            limit = 8

        search_query = params.get('search', '').strip()
        selected_type = params.get('type', 'all')
        date_from = params.get('dateFrom', '')
        date_to = params.get('dateTo', '')

        # Base Domain: Always ensure the operation is validated and done
        domain = [('state', '=', 'done'), *self._get_company_domain()]

        # 2. Dynamic Filtering
        # A) Date Filters
        if date_from:
            domain.append(('date', '>=', f"{date_from} 00:00:00"))
        if date_to:
            domain.append(('date', '<=', f"{date_to} 23:59:59"))

        if search_query:
            domain.extend([
                '|', '|', '|',
                ('product_id.name', 'ilike', search_query),
                ('product_id.default_code', 'ilike', search_query),
                ('location_id.complete_name', 'ilike', search_query),
                ('location_dest_id.complete_name', 'ilike', search_query)
            ])

        all_lines = self.search(domain, order='date desc')

        formatted_movements = []
        for line in all_lines:
            src_name = line.location_id.complete_name or ''
            dest_name = line.location_dest_id.complete_name or ''
            
            # Support Odoo 16 (qty_done) and Odoo 17/18 (quantity)
            raw_qty = getattr(line, 'quantity', getattr(line, 'qty_done', 0))
            qty = round(raw_qty)

            # --- Your Precise Location String Parser ---
            if "Virtual" in src_name or "Partner" in src_name:
                move_type = "incoming"
                direction = "إدخال مخزني"
                badge_color = "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                calculated_qty = qty  # Positive quantity
            elif "Virtual" in dest_name or "Partner" in dest_name or "Customer" in dest_name:
                move_type = "outgoing"
                direction = "صرف مخزني"
                badge_color = "bg-rose-500/10 text-rose-400 border border-rose-500/20"
                calculated_qty = -abs(qty)  # Negative quantity
            else:
                move_type = "transfer"
                direction = "تحويل داخلي"
                badge_color = "bg-blue-500/10 text-blue-400 border border-blue-500/20"
                calculated_qty = qty

            # Frontend Type Filtering Match Execution
            if selected_type != 'all' and selected_type != move_type:
                continue

            # Datetime Localized Parsing
            dt_field = fields.Datetime.from_string(line.date)
            local_dt = fields.Datetime.context_timestamp(self, dt_field) if self.env.user else dt_field

            formatted_movements.append({
                'id': line.picking_id.name or line.reference or f"ST-LV-{line.id}",
                'date': local_dt.strftime('%Y-%m-%d'),
                'time': local_dt.strftime('%H:%M:%S'),
                'productName': line.product_id.display_name,
                'sku': line.product_id.default_code or '',
                'type': move_type,              # Matches frontend condition ('incoming' | 'outgoing' | 'transfer')
                'typeLabel': direction,         # Arabic Label matching your style
                'fromLocation': src_name,
                'toLocation': dest_name,
                'qty': calculated_qty,          # Evaluated number with positive/negative mathematical assignment
                'operator': line.create_uid.name or 'النظام',
                'status': 'done',
                'statusLabel': 'مكتمل',
                'badgeColor': badge_color       # Passes your custom styling strings down seamlessly
            })

        # 4. In-Memory Slice Pagination Engine
        total_items = len(formatted_movements)
        total_pages = math.ceil(total_items / limit) if total_items > 0 else 1
        
        offset_start = (page - 1) * limit
        offset_end = offset_start + limit
        paginated_data = formatted_movements[offset_start:offset_end]

        return {
            'success': True,
            'totalItems': total_items,
            'totalPages': total_pages,
            'currentPage': page,
            'itemsPerPage': limit,
            'data': paginated_data
        }