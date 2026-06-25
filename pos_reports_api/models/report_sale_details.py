# -*- coding: utf-8 -*-
from odoo import models, api

class ReportSaleDetails(models.AbstractModel):
    _inherit = 'report.point_of_sale.report_saledetails'

    @api.model
    def get_sale_details(self, date_start=False, date_stop=False, config_ids=False, session_ids=False, **kwargs):
        res = super(ReportSaleDetails, self).get_sale_details(date_start, date_stop, config_ids, session_ids, **kwargs)
        
        # Ensure values that are 'dict_values' are converted to list 
        # so xmlrpc doesn't fail to serialize
        if 'taxes' in res and not isinstance(res['taxes'], list):
            res['taxes'] = list(res['taxes'])
        if 'refund_taxes' in res and not isinstance(res['refund_taxes'], list):
            res['refund_taxes'] = list(res['refund_taxes'])
        if 'payments_per_method' in res and not isinstance(res['payments_per_method'], list):
            res['payments_per_method'] = list(res['payments_per_method'])
            
        return res
