{
    "name": "POS Reports API",
    "version": "1.0",
    "category": "Custom",
    "author": "Ahmed Moftah",
    "depends": [
        "base", "sale", "account", "stock", "stock_account",
        "point_of_sale",
        "pos_data_controller", "dashboard_kpi", "pos_stock_ledger",
        "pos_customer_ledger", "pos_vendor_bills", "custom_order_api",
        "custom_warehouse",
    ],
    "data": ["security/ir.model.access.csv"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
