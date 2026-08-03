import math

from odoo import api, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _compute_is_driver(self):
        for partner in self:
            partner.is_driver = bool(partner.carrier_ids) or partner.is_driver

    @api.model
    def get_pos_frontend_drivers(self, params=None):
        if params is None:
            params = {}

        try:
            page = max(1, int(params.get('page', 1)))
        except (ValueError, TypeError):
            page = 1

        limit = 20
        offset = (page - 1) * limit
        search_query = (params.get('search') or '').strip()

        domain = [('is_driver', '=', True)]
        if search_query:
            domain.extend([
                '|', '|',
                ('name', 'ilike', search_query),
                ('phone', 'ilike', search_query),
                ('email', 'ilike', search_query),
            ])

        total_count = self.search_count(domain)
        total_pages = math.ceil(total_count / limit) if total_count > 0 else 1
        partners = self.search(domain, offset=offset, limit=limit, order='create_date desc')

        carrier_map = {}
        carriers = self.env['delivery.carrier'].search([('driver_id', 'in', partners.ids)])
        for carrier in carriers:
            carrier_map.setdefault(carrier.driver_id.id, []).append(carrier.name)

        data = []
        for p in partners:
            data.append({
                'id': p.id,
                'name': p.name or '',
                'email': p.email or '',
                'phone': p.mobile or p.phone or '',
                'carriers': carrier_map.get(p.id, []),
                'createdAt': str(p.create_date)[:10] if p.create_date else '',
            })

        return {
            'success': True,
            'totalItems': total_count,
            'totalPages': total_pages,
            'currentPage': page,
            'itemsPerPage': limit,
            'data': data,
        }

    @api.model
    def create_pos_frontend_driver(self, params=None):
        if params is None:
            params = {}

        partner_vals = {}
        if params.get('name'):
            partner_vals['name'] = params['name']
        if params.get('email'):
            partner_vals['email'] = params['email']
        if params.get('phone'):
            partner_vals['mobile'] = params['phone']
            partner_vals['phone'] = params['phone']

        if not partner_vals.get('name'):
            return {'success': False, 'message': 'اسم السائق مطلوب'}

        partner_vals['is_driver'] = True

        if params.get('id'):
            partner = self.browse(int(params['id']))
            if not partner.exists():
                return {'success': False, 'message': 'سجل السائق غير موجود'}
            partner.write(partner_vals)
            partner.is_driver = True
            return {'success': True, 'id': partner.id, 'message': 'تم تحديث بيانات السائق بنجاح'}

        partner = self.create(partner_vals)
        partner.is_driver = True
        return {'success': True, 'id': partner.id, 'message': 'تم إنشاء السائق بنجاح'}
