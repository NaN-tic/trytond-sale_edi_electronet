# -*- coding: utf-8 -*
# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool, PoolMeta
from trytond.model import fields
from trytond.modules.product import price_digits
from edifact.message import Message
from edifact.serializer import Serializer
from edifact.utils import (RewindIterator, with_segment_check,
    separate_section, validate_segment, DO_NOTHING, NO_ERRORS)
from edifact.errors import (IncorrectValueForField, MissingFieldsError)
import oyaml as yaml
from io import open
import os
from datetime import datetime
from itertools import chain
from decimal import Decimal


__all__ = ['Sale', 'SaleLine', 'Cron']

ZERO_ = Decimal('0')
NO_SALE = None
KNOWN_EXTENSIONS = ['.txt', '.edi', '.pla']
MODULE_PATH = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = 'templates/ORDERS.yml'
UOMS_EDI_TO_TRYTON = {
    'KGM': 'kg',
    'LTR': 'l',
    'MTR': 'm',
    'UN': 'u',
}

class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super(Cron, cls).__setup__()
        cls.method.selection.extend([
            ('sale.sale|get_sales_from_edi_files_cron', 'Create EDI Orders')])

class Sale(metaclass=PoolMeta):
    __name__ = 'sale.sale'

    edi_order_file = fields.Binary('EDI Order File', states={
            'readonly': True,
            })

    @classmethod
    def create_sale_from_edi_file(cls, edi_file, template_name):
        """
        Creates a sale record from a given edi file
        :param edi_file: EDI file to be processed.
        :template_name: File name from the file used to validate the EDI msg.
        """
        pool = Pool()
        SaleLine = pool.get('sale.line')

        template_path = os.path.join(MODULE_PATH, template_name)
        with open(template_path, encoding='utf-8') as fp:
            template = yaml.load(fp.read())
        message = Message.from_file(edi_file, encoding='utf-8')
        # If there isn't a segment UNH with ORDERS:D:96A:UN:EAN008
        # means the file readed it's not a EDI order.
        unh = message.get_segment('UNH')
        if (not unh or u"ORDERS:D:96A:UN:EAN008"
                not in Serializer().serialize([unh])):
            return NO_SALE, NO_ERRORS

        segments_iterator = RewindIterator(message.segments)
        header = [x for x in chain(*separate_section(segments_iterator,
                    end='LIN'))]
        detail = [x for x in separate_section(segments_iterator, start='LIN',
                end='UNS')]
        del(segments_iterator)

        total_errors = []
        discard_if_partial_sale = False
        values = {}
        for segment in header:
            # Ignore the tags we not use
            if segment.tag not in template['header'].keys():
                continue
            template_segment = template['header'].get(segment.tag)
            # Segment ALI has a special management, it doesn't provides
            # any value for the sale but defines if the sale will be created
            # if some requested products can't not be selled.
            if segment.tag == 'ALI':
                discard_if_partial_sale, errors = cls._process_ALI(
                    segment, template)
                if errors:
                    total_errors += errors
                continue

            process = eval('cls._process_{}'.format(segment.tag))
            to_update, errors = process(segment, template_segment)
            if errors:
                total_errors += errors
                continue
            if to_update:
                values.update(to_update)

        # If any header segment could be processed or there isn't a party
        # the sale isn't created
        if not values or not values.get('shipment_party'):
            return NO_SALE, total_errors

        sale = cls()
        for k, v in values.items():
            setattr(sale, k, v)
        sale.on_change_shipment_party()
        sale.on_change_party()
        lines = []
        for linegroup in detail:
            values = {}
            for segment in linegroup:
                if segment.tag not in template['detail'].keys():
                    continue
                template_segment = template['detail'].get(segment.tag)
                process = eval('cls._process_{}LIN'.format(segment.tag))
                to_update, errors = process(segment, template_segment)
                if errors:
                    # If there are errors the linegroup isn't processed
                    total_errors += errors
                    break
                if to_update:
                    values.update(to_update)
            if errors:
                continue
            line = SaleLine().set_fields_value(values)
            # This fields are a required fields, we set its value to a
            # default valuein order to the sale can be saved. No matter
            # if it isn't the true value because it will be calculated next
            # in the on_change_product and on_change_quantity calls.
            if not hasattr(line, 'description'):
                line.description = 'temp EDI description'
            if not hasattr(line, 'unit_price'):
                line.unit_price = ZERO_
            else:
                # If the line has a unit_price it means that isn't necessary
                # to apply discounts and it must be cleaned.
                if hasattr(line, 'discount1'):
                    line.discount1 = ZERO_
                elif hasattr(line, 'discount'):
                    line.discount = ZERO_

            lines.append(line)

        if lines:
            sale.lines = lines
        return sale, total_errors

    @classmethod
    @with_segment_check
    def _process_BGM(cls, segment, template):
        return {'reference': segment.elements[1]}, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_ALI(cls, segment, template):
        return DO_NOTHING, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_FTX(cls, segment, template):
        return {'comment': segment.elements[3]}, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_NAD(cls, segment, template):
        serializer = Serializer()
        pool = Pool()
        PartyIdentifier = pool.get('party.identifier')
        if segment.elements[0] in ('MS',):
            edi_operational_point = segment.elements[1][0]
            identifier = PartyIdentifier.search([
                    ('type', '=', 'edi'),
                    ('code', '=', edi_operational_point)],
                limit=1)
            if not identifier:
                serialized_segment = serializer.serialize([segment])
                msg = 'Party not found'
                return DO_NOTHING, ['{}: {}'.format(msg, serialized_segment)]
            party = identifier[0].party
            return {'shipment_party': party, 'party': party}, NO_ERRORS

        return DO_NOTHING, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_CUX(cls, segment, template):
        pool = Pool()
        serializer = Serializer()
        Currency = pool.get('currency.currency')
        currency_code = segment.elements[0][2]
        currency = Currency.search([('code', '=', currency_code)], limit=1)
        if not currency:
            serialized_segment = serializer.serialize([segment])
            msg = 'Currency not found'
            return DO_NOTHING, ['{}: {}'.format(msg, serialized_segment)]
        currency, = currency
        return {'currency': currency}, NO_ERRORS

    @classmethod
    def _process_PIALIN(cls, segment, template):
        pool = Pool()
        Product = pool.get('product.product')
        try:
            validate_segment(segment.elements, template)
        except MissingFieldsError:
            return DO_NOTHING, NO_ERRORS
        except IncorrectValueForField:
            serializer = Serializer()
            serialized_segment = serializer.serialize([segment])
            msg = 'Incorrect value for field in segment'
            return DO_NOTHING, ['{}: {}'.format(
                        msg, str(serialized_segment))]
        else:
            code = segment.elements[1][0]
            product = Product.search([('code', '=', code)], limit=1)
            if not product:
                serializer = Serializer()
                serialized_segment = serializer.serialize([segment])
                msg = 'No product found in segment'
                return DO_NOTHING, ['{}: {}'.format(
                        msg, str(serialized_segment))]
            return {'product': product[0].id}, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_QTYLIN(cls, segment, template):
        pool = Pool()
        Uom = pool.get('product.uom')
        uom_value = UOMS_EDI_TO_TRYTON.get(segment.elements[0][-1], 'u')
        uom, = Uom.search([('symbol', '=', uom_value)], limit=1)
        quantity = float(segment.elements[0][2])
        return {'unit': uom, 'quantity': quantity}, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_DTMLIN(cls, segment, template):
        date = datetime.strptime(segment.elements[0][2], '%Y%m%d')
        return {'shipping_date': date}, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_PRILIN(cls, segment, template):
        pool = Pool()
        SaleLine = pool.get('sale.line')
        field = None
        value = segment.elements[0][2]
        if segment.elements[0][0] == 'AAA':
            field = 'unit_price'
        elif segment.elements[0][0] == 'AAB':
            # If the model SaleLine doesn't have the field gross_unit_price
            # means the module sale_discount was not installed.
            if hasattr(SaleLine, 'gross_unit_price'):
                field = 'gross_unit_price'
        if not field:
            return DO_NOTHING, NO_ERRORS
        value = Decimal(value).quantize(Decimal(1) / 10 ** price_digits[1])
        return {field: Decimal(value)}, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_PCDLIN(cls, segment, template):
        pool = Pool()
        SaleLine = pool.get('sale.line')
        field = None
        discount = Decimal(segment.elements[0][2]) / 100
        # If the model SaleLine doesn't have the field discount1 means
        # the module sale_3_discounts was not installed.
        if hasattr(SaleLine, 'discount1'):
            field = 'discount1'
        # If the model SaleLine doesn't have the field discount means
        # the module sale_discount was not installed.
        elif hasattr(SaleLine, 'discount'):
            field = 'discount'
        else:
            return DO_NOTHING, NO_ERRORS

        return {field: discount}, NO_ERRORS

    @classmethod
    def create_sales_from_edi_files(cls, template=DEFAULT_TEMPLATE):
        """
        Get sales from edi files
        """
        pool = Pool()
        Configuration = pool.get('sale.configuration')
        configuration = Configuration(1)
        errors_path = os.path.abspath(configuration.edi_errors_path)
        source_path = os.path.abspath(configuration.edi_source_path)
        files = [os.path.join(source_path, file) for file in
                 os.listdir(source_path) if os.path.isfile(os.path.join(
                     source_path, file))]
        sales = []
        to_delete = []
        for fname in files:
            if fname[-4:] not in KNOWN_EXTENSIONS:
                continue
            try:
                sale, errors = cls.create_sale_from_edi_file(
                    fname, template)
            except RuntimeError:
                continue
            else:
                if sale:
                    with open(fname, 'rb') as fp:
                        sale.edi_order_file = fp.read()
                    sales.append(sale)
                    to_delete.append(fname)
                if errors:
                    error_fname = os.path.join(errors_path,
                        'error_{}.EDI'.format(
                            os.path.splitext(os.path.basename(fname))[0]
                            ))
                    with open(error_fname, 'w') as fp:
                        fp.write('\n'.join(errors))
        results = cls.create([s._save_values for s in sales]) if sales else []
        if to_delete:
            for file in to_delete:
                os.remove(file)
        return results

    @classmethod
    def apply_on_change_product_and_quantity_to_lines(cls, sales):
        pool = Pool()
        SaleLine = pool.get('sale.line')
        to_write = []
        for sale in sales:
            for line in sale.lines:
                line.apply_on_change_product_and_quantity()
                to_write.extend(([line], line._save_values))
        if to_write:
            SaleLine.write(*to_write)

    @classmethod
    def get_sales_from_edi_files(cls):
        '''Get orders from edi files'''
        results = cls.create_sales_from_edi_files()
        cls.apply_on_change_product_and_quantity_to_lines(results)
        return results

    @classmethod
    def get_sales_from_edi_files_cron(cls):
        """
        Cron get orders from edi files:
        - State: active
        """
        cls.get_sales_from_edi_files()
        return True


class SaleLine(metaclass=PoolMeta):
    __name__ = 'sale.line'

    def set_fields_value(self, values):
        """
        Set SaleLine fields values from a given dict
        """
        for k, v in values.items():
            setattr(self, k, v)
        return self

    def apply_on_change_product_and_quantity(self):
        self.on_change_product()
        self.on_change_quantity()
