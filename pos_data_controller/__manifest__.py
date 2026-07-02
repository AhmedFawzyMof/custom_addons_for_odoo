{
    'name': 'POS API Controller',
    'version': '1.0',
    'category': 'Point of Sale',
    'depends': [
        'point_of_sale', 
        'stock'
    ],
    'data': [
        'views/pos_config_views.xml',
    ],
    'installable': True,
    'application': False,
}