
# -*- coding: utf-8 -*
# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.model import fields
from trytond.pool import PoolMeta


class SaleConfiguration(metaclass=PoolMeta):
    __name__ = 'sale.configuration'

    edi_source_path = fields.Char('Source Path')
    edi_errors_path = fields.Char('Errors Path')
    template_sale_edi = fields.Char('Template EDI Used for Sale')

    @staticmethod
    def default_edi_source_path():
        return '/tmp/'

    @staticmethod
    def default_edi_errors_path():
        return '/tmp/'
