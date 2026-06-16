from odoo import models, fields, api, _
from odoo.exceptions import UserError


class OperationalExpenseApi(models.AbstractModel):
    _name = 'operational.expense.api'
    _description = 'Operational Expense API Handler'

    CATEGORY_MAP = {
        'rent': 'إيجار',
        'electricity': 'كهرباء',
        'water': 'مياه',
        'internet': 'إنترنت',
        'salaries': 'رواتب',
        'marketing': 'تسويق',
        'maintenance': 'صيانة',
        'transport': 'نقل',
        'legal': 'قانوني واستشاري',
        'office': 'لوازم مكتبية',
        'insurance': 'تأمين',
        'cleaning': 'تنظيف',
        'taxes': 'ضرائب ورسوم',
        'other': 'أخرى',
    }

    def _get_company_domain(self):
        cids = self.env.context.get('allowed_company_ids', [])
        if cids:
            return [('company_id', 'in', cids)]
        return []

    @api.model
    def get_operational_expenses(self, params=None):
        if params is None:
            params = {}
        try:
            page = max(1, int(params.get('page', 1)))
            limit = max(1, int(params.get('limit', 20)))
        except (ValueError, TypeError):
            page = 1
            limit = 20
        offset = (page - 1) * limit
        search_term = params.get('search', '').strip()
        category = params.get('category', '')
        date_from = params.get('date_from', '')
        date_to = params.get('date_to', '')
        state = params.get('state', '')

        domain = self._get_company_domain()
        if search_term:
            domain = ['|', ('name', 'ilike', search_term), ('notes', 'ilike', search_term)] + domain
        if category:
            domain.append(('category', '=', category))
        if date_from:
            domain.append(('date', '>=', date_from))
        if date_to:
            domain.append(('date', '<=', date_to))
        if state:
            domain.append(('state', '=', state))

        total_count = self.env['operational.expense'].search_count(domain)
        total_pages = (total_count + limit - 1) // limit if total_count > 0 else 1
        expenses = self.env['operational.expense'].search(
            domain, offset=offset, limit=limit, order='date desc, id desc'
        )

        expense_list = []
        for e in expenses:
            expense_list.append({
                'id': e.id,
                'name': e.name,
                'amount': e.amount,
                'category': e.category,
                'category_label': self.CATEGORY_MAP.get(e.category, e.category),
                'date': e.date.strftime('%Y-%m-%d') if e.date else '',
                'notes': e.notes or '',
                'state': e.state,
                'move_id': e.move_id.id if e.move_id else False,
                'move_name': e.move_id.name if e.move_id else '',
                'journal_id': e.journal_id.id if e.journal_id else False,
                'journal_name': e.journal_id.name if e.journal_id else '',
                'company_id': [e.company_id.id, e.company_id.name] if e.company_id else False,
            })

        return {
            'success': True,
            'totalItems': total_count,
            'totalPages': total_pages,
            'currentPage': page,
            'itemsPerPage': limit,
            'data': expense_list,
        }

    @api.model
    def create_operational_expense(self, payload):
        name = payload.get('name', '').strip()
        amount = payload.get('amount', 0)
        category = payload.get('category', 'other')
        date = payload.get('date', fields.Date.today())
        notes = payload.get('notes', '')

        if not name:
            return {'success': False, 'message': 'اسم المصروف مطلوب'}
        try:
            amount = float(amount)
        except (ValueError, TypeError):
            return {'success': False, 'message': 'المبلغ غير صحيح'}
        if amount <= 0:
            return {'success': False, 'message': 'المبلغ يجب أن يكون أكبر من صفر'}

        if category not in dict(self.env['operational.expense']._fields['category'].selection):
            return {'success': False, 'message': 'التصنيف غير صحيح'}

        journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not journal:
            return {'success': False, 'message': 'لا يوجد دفتر يومية عام. قم بإنشاء واحد أولاً.'}

        try:
            expense = self.env['operational.expense'].create({
                'name': name,
                'amount': amount,
                'category': category,
                'date': date,
                'notes': notes,
                'journal_id': journal.id,
            })
            expense.action_post()
            return {
                'success': True,
                'expense_id': expense.id,
                'name': expense.name,
                'message': 'تم تسجيل المصروف بنجاح',
            }
        except UserError as e:
            return {'success': False, 'message': str(e)}
        except Exception as e:
            return {'success': False, 'message': f'فشل في تسجيل المصروف: {str(e)}'}

    @api.model
    def get_expense_categories(self):
        categories = []
        for key, label in self.env['operational.expense']._fields['category'].selection:
            categories.append({
                'id': key,
                'name': self.CATEGORY_MAP.get(key, label),
            })
        return {'success': True, 'data': categories}
