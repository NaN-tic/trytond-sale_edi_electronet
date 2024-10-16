# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool
from . import configuration
from . import sale


def register():
    Pool.register(
        configuration.SaleConfiguration,
        sale.Sale,
        sale.SaleLine,
        sale.Cron,
        module='sale_edi_electronet', type_='model')
