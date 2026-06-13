from odoo import models, api
import datetime

class WarehouseLocation(models.AbstractModel):
    _name = 'warehouse.location.api'
    _description = 'Warehouse Location API'

    def _get_company_domain(self):
        cids = self.env.context.get('allowed_company_ids', [])
        if cids:
            return [('company_id', 'in', cids)]
        return []

    @api.model
    def get_locations_with_capacity(self):
        _cids = self.env.context.get('allowed_company_ids', [])
        locations = self.env['stock.location'].search_read(
            [['usage', '=', 'internal'], ['active', '=', True]]
            + ([('company_id', 'in', _cids)] if _cids else []),
            fields=['id', 'name', 'complete_name', 'location_id']
        )

        location_ids = [l['id'] for l in locations]

        quants = self.env['stock.quant'].search_read(
            [['location_id', 'in', location_ids]],
            fields=['location_id', 'quantity']
        )

        qty_map = {}
        for quant in quants:
            loc_id = quant['location_id'][0]
            qty_map[loc_id] = qty_map.get(loc_id, 0) + quant['quantity']

        MAX_CAPACITY = 5000
        result = []
        for loc in locations:
            total_qty = round(qty_map.get(loc['id'], 0))
            percentage = min(round((total_qty / MAX_CAPACITY) * 100), 100)
            is_full = percentage >= 90

            result.append({
                'id': loc['id'],
                'name': f"{loc['location_id'][1]} / المخزن الأساسي"
                        if loc['name'] == 'Stock' and loc['location_id']
                        else loc['name'],
                'address': loc['complete_name'],
                'status': 'كامل السعة' if is_full else 'نشط',
                'statusColor': 'bg-secondary-container/20 text-secondary' if is_full else 'bg-primary-container/20 text-primary',
                'qty': f"{total_qty:,}",
                'capacity': f"{percentage}%",
                'capacityWidth': f"w-[{percentage}%]",
                'progressBarColor': 'bg-error' if is_full else 'bg-primary',
            })

        return result

    @api.model
    def get_recent_movements(self):
        """
        Fetches the 5 most recent done stock movements, parses transaction 
        directions, and prepares optimized Arabic metadata payloads.
        """
        _cids = self.env.context.get('allowed_company_ids', [])
        domain = [('state', '=', 'done')]
        if _cids:
            domain.append(('company_id', 'in', _cids))
        # Swap 'qty_done' with 'quantity' in the fields array
        move_lines = self.env['stock.move.line'].search_read(
            domain=domain,
            fields=['id', 'reference', 'product_id', 'location_id', 'location_dest_id', 'quantity', 'date'],
            order='date desc',
            limit=5
        )

        recent_moves = []
        for line in move_lines:
            src_usage = line['location_id'][1] if line['location_id'] else ''
            dest_usage = line['location_dest_id'][1] if line['location_dest_id'] else ''
            
            # Read from the updated 'quantity' key here
            qty = round(line['quantity'])
            
            # --- Determine Movement Type Direction & Colors Natively ---
            if "Virtual" in src_usage or "Partner" in src_usage:
                direction = "إدخال مخزني"
                direction_type = "inbound"
                badge_color = "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                prefix = "+"
            elif "Virtual" in dest_usage or "Partner" in dest_usage or "Customer" in dest_usage:
                direction = "صرف مخزني"
                direction_type = "outbound"
                badge_color = "bg-rose-500/10 text-rose-400 border border-rose-500/20"
                prefix = "-"
            else:
                direction = "تحويل داخلي"
                direction_type = "internal"
                badge_color = "bg-blue-500/10 text-blue-400 border border-blue-500/20"
                prefix = ""

            raw_date = line['date']

            recent_moves.append({
                'id': line['id'],
                'reference': line['reference'] or 'بدون مرجع',
                'product': line['product_id'][1] if line['product_id'] else 'منتج غير معروف',
                'source': line['location_id'][1] if line['location_id'] else 'غير محدد',
                'destination': line['location_dest_id'][1] if line['location_dest_id'] else 'غير محدد',
                'qty': f"{prefix}{qty:,}",
                'type': direction,
                'typeClass': direction_type,
                'badgeColor': badge_color,
                'time': raw_date
            })

        return recent_moves
        
    @api.model
    def get_top_sold_stock_levels(self, page=1, limit=5):
        """
        Aligned to accept strict positional values directly from the client array.
        """
        # Ensure fallback safety conversions
        try:
            page = max(int(page), 1)
        except (ValueError, TypeError):
            page = 1

        try:
            limit = max(int(limit), 1)
        except (ValueError, TypeError):
            limit = 5

        offset = (page - 1) * limit

        _cids = self.env.context.get('allowed_company_ids', [])
        # 1. Query the Sale Report to get product IDs sorted by total volume sold
        sales_data = self.env['sale.report'].read_group(
            domain=[['state', 'in', ['sale', 'done']]]
            + ([('company_id', 'in', _cids)] if _cids else []),
            fields=['product_id', 'product_uom_qty'],
            groupby=['product_id'],
            orderby='product_uom_qty desc'
        )

        if not sales_data:
            return {'total_records': 0, 'products': [], 'page': page, 'total_pages': 0}

        total_records = len(sales_data)
        total_pages = (total_records + limit - 1) // limit

        # Slice the sales dataset manually to respect page pagination offsets
        paginated_sales = sales_data[offset:offset + limit]
        top_product_ids = [item['product_id'][0] for item in paginated_sales if item['product_id']]

        # 2. Fetch real-time quantity profiles for these targeted products
        products = self.env['product.product'].browse(top_product_ids)
        
        # Build an easy data mapper dictionary
        sales_qty_map = {item['product_id'][0]: item['product_uom_qty'] for item in paginated_sales if item['product_id']}

        result_products = []
        for prod in products:
            qty_available = round(prod.qty_available)
            sold_qty = round(sales_qty_map.get(prod.id, 0))
            
            is_low_stock = qty_available <= 10
            is_out_of_stock = qty_available <= 0

            if is_out_of_stock:
                status_text = "نفد المخزون"
                status_color = "text-rose-400 bg-rose-500/10 border border-rose-500/20"
            elif is_low_stock:
                status_text = "مخزون منخفض"
                status_color = "text-amber-400 bg-amber-500/10 border border-amber-500/20"
            else:
                status_text = "متوفر"
                status_color = "text-emerald-400 bg-emerald-500/10 border border-emerald-500/20"

            result_products.append({
                'id': prod.id,
                'name': prod.display_name or 'منتج غير مسمى',
                'barcode': prod.barcode or 'لا يوجد باركود',
                'total_sold': f"{sold_qty:,}",
                'qty_available': f"{qty_available:,}",
                'raw_qty': qty_available,
                'status': status_text,
                'statusClass': status_color,
            })

        return {
            'total_records': total_records,
            'page': page,
            'total_pages': total_pages,
            'products': result_products
        }