
# -*- coding: utf-8 -*
# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.model import fields
from trytond.pool import PoolMeta

__all__ = ['SaleConfiguration']


class SaleConfiguration:
    __metaclass__ = PoolMeta
    __name__ = 'sale.configuration'

    edi_source_path = fields.Char('Source Path')
    edi_errors_path = fields.Char('Errors Path')

    @staticmethod
    def default_edi_source_path():
        return u'/tmp/'

    @staticmethod
    def default_edi_errors_path():
        return u'/tmp/'
