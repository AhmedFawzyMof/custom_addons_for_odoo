import math
import pytz
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
        selected_usage = params.get('usage', '')
        date_from = params.get('dateFrom', '')
        date_to = params.get('dateTo', '')
        product_id = params.get('productId', '')

        if product_id:
            try:
                product_id = int(product_id)
            except (ValueError, TypeError):
                product_id = None

        # Base Domain: Always ensure the operation is validated and done
        domain = [('state', '=', 'done'), *self._get_company_domain()]

        # 2. Dynamic Filtering
        # A) Date Filters
        if date_from:
            domain.append(('date', '>=', f"{date_from} 00:00:00"))
        if date_to:
            domain.append(('date', '<=', f"{date_to} 23:59:59"))

        # B) Product Filter (product.template id → variant's product_tmpl_id)
        if product_id:
            domain.append(('product_id.product_tmpl_id', '=', product_id))

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
            src_usage = line.location_id.usage or ''
            dest_usage = line.location_dest_id.usage or ''
            
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

            # Usage-based filter (e.g. only internal-to-internal transfers)
            if selected_usage == 'internal' and (src_usage != 'internal' or dest_usage != 'internal'):
                continue

            # Egypt Timezone Conversion
            dt_field = fields.Datetime.from_string(line.date)
            cairo_tz = pytz.timezone('Africa/Cairo')
            local_dt = pytz.utc.localize(dt_field).astimezone(cairo_tz)

            # Determine source document type
            picking = line.picking_id
            origin = ''
            if picking:
                if picking.sale_id:
                    origin = 'طلب بيع (RPC)'
                elif picking.purchase_id:
                    origin = 'أمر شراء (PO)'
                elif picking.origin and 'جرد' in picking.origin:
                    origin = 'جرد مخزني'
                elif picking.origin:
                    origin = picking.origin
                else:
                    origin = 'تحويل داخلي'
            else:
                origin = 'إدخال يدوي'

            formatted_movements.append({
                'id': line.picking_id.name or line.reference or f"ST-LV-{line.id}",
                'date': local_dt.strftime('%Y-%m-%d'),
                'time': local_dt.strftime('%H:%M:%S'),
                'productName': line.product_id.display_name,
                'sku': line.product_id.default_code or '',
                'type': move_type,
                'typeLabel': direction,
                'origin': origin,
                'fromLocation': src_name,
                'toLocation': dest_name,
                'fromUsage': src_usage,
                'toUsage': dest_usage,
                'qty': calculated_qty,
                'operator': line.create_uid.name or 'النظام',
                'status': 'done',
                'statusLabel': 'مكتمل',
                'badgeColor': badge_color,
                'reference': line.reference or line.picking_id.name or '',
                'notes': line.move_id.description_picking or ''
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