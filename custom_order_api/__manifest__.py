{
    'name': 'Custom Order API Model',
    'version': '1.0',
    'category': 'Sales',
    'summary': 'Exposes Order management logic via model methods for headless APIs',
    'depends': ['sale', 'account', 'point_of_sale'],
    'data': ['security/ir.model.access.csv'],
    'installable': True,
    'application': True,  
}