{
    'name': 'POS Vendor Bills & Purchase Orders',
    'version': '1.0',
    'category': 'Point of Sale / Purchase',
    'depends': [
        'purchase',
        'account',
        'stock',
        'base',
        'account_invoice_supplier_ref_unique',
        'account_invoice_supplierinfo_update',
    ],
    'data': [],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}