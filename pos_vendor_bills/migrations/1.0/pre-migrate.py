def migrate(cr, version):
    cr.execute("""
        ALTER TABLE operational_expense
        ALTER COLUMN expense_account_id DROP NOT NULL,
        ALTER COLUMN payment_account_id DROP NOT NULL
    """)
