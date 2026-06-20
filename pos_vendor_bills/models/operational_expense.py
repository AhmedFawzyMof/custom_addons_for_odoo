from odoo import models, fields, api, _
from odoo.exceptions import UserError


class OperationalExpense(models.Model):
    _name = 'operational.expense'
    _description = 'Operational Expense'
    _order = 'date desc, id desc'

    name = fields.Char(string='Description', required=True)
    amount = fields.Monetary(string='Amount', required=True)
    category = fields.Selection([
        ('rent', 'Rent'),
        ('electricity', 'Electricity'),
        ('water', 'Water'),
        ('internet', 'Internet'),
        ('salaries', 'Salaries'),
        ('marketing', 'Marketing'),
        ('maintenance', 'Maintenance'),
        ('transport', 'Transport'),
        ('legal', 'Legal & Professional'),
        ('office', 'Office Supplies'),
        ('insurance', 'Insurance'),
        ('cleaning', 'Cleaning'),
        ('taxes', 'Taxes & Fees'),
        ('other', 'Other'),
    ], string='Category', required=True, default='other')
    date = fields.Date(string='Date', required=True, default=fields.Date.today)
    notes = fields.Text(string='Notes')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancel', 'Cancelled'),
    ], string='Status', default='draft')
    company_id = fields.Many2one('res.company', string='Company', required=True,
                                  default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id',
                                   string='Currency')
    journal_id = fields.Many2one('account.journal', string='Journal',
                                  domain=[('type', '=', 'general')],
                                  required=True)
    move_id = fields.Many2one('account.move', string='Journal Entry', readonly=True)
    expense_account_id = fields.Many2one('account.account', string='Expense Account',
                                          domain=[('account_type', 'in', ('expense', 'expense_depreciation', 'expense_direct_cost'))],
                                          required=False)
    payment_account_id = fields.Many2one('account.account', string='Payment Account',
                                          domain=[('account_type', 'in', ('asset_cash', 'asset_current', 'liability_current'))],
                                          required=False)

    @api.model
    def _get_default_expense_journal(self):
        journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        return journal

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        journal = self._get_default_expense_journal()
        if journal:
            res['journal_id'] = journal.id
        return res

    @api.model
    def _get_expense_account(self, category):
        mapping = {
            'rent': 'expense',
            'electricity': 'expense',
            'water': 'expense',
            'internet': 'expense',
            'salaries': 'expense',
            'marketing': 'expense',
            'maintenance': 'expense',
            'transport': 'expense',
            'legal': 'expense',
            'office': 'expense',
            'insurance': 'expense',
            'cleaning': 'expense',
            'taxes': 'expense',
            'other': 'expense',
        }
        acct_type = mapping.get(category, 'expense')
        account = self.env['account.account'].search([
            ('account_type', '=', acct_type),
            ('company_ids', '=', self.env.company.id),
            ('deprecated', '=', False),
        ], limit=1)
        return account

    def action_post(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft expenses can be posted.'))
            if not rec.expense_account_id:
                account = rec._get_expense_account(rec.category)
                if not account:
                    raise UserError(_('No expense account found for category %s') % rec.category)
                rec.expense_account_id = account
            if not rec.payment_account_id:
                cash_account = self.env['account.account'].search([
                    ('account_type', '=', 'asset_cash'),
                    ('company_ids', '=', rec.company_id.id),
                    ('deprecated', '=', False),
                ], limit=1)
                if not cash_account:
                    cash_account = self.env['account.account'].search([
                        ('account_type', '=', 'asset_current'),
                        ('company_ids', '=', rec.company_id.id),
                        ('deprecated', '=', False),
                    ], limit=1)
                if not cash_account:
                    raise UserError(_('No cash or current asset account found. Configure one first.'))
                rec.payment_account_id = cash_account
            if not rec.journal_id:
                raise UserError(_('A journal is required to post the expense.'))

            move_vals = {
                'journal_id': rec.journal_id.id,
                'date': rec.date,
                'ref': rec.name,
                'line_ids': [
                    (0, 0, {
                        'name': rec.name,
                        'account_id': rec.expense_account_id.id,
                        'debit': rec.amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': rec.name,
                        'account_id': rec.payment_account_id.id,
                        'debit': 0.0,
                        'credit': rec.amount,
                    }),
                ],
            }
            move = self.env['account.move'].create(move_vals)
            move.action_post()
            rec.write({
                'state': 'posted',
                'move_id': move.id,
            })
        return True

    def action_draft(self):
        for rec in self:
            if rec.state == 'posted' and rec.move_id:
                rec.move_id.button_draft()
                rec.move_id.button_cancel()
                rec.write({'state': 'draft', 'move_id': False})
        return True

    def action_cancel(self):
        for rec in self:
            if rec.state == 'posted' and rec.move_id:
                rec.move_id.button_draft()
                rec.move_id.button_cancel()
            rec.write({'state': 'cancel', 'move_id': False})
        return True
