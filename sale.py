# -*- coding: utf-8 -*
# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool, PoolMeta
from trytond.modules.product import price_digits
from trytond.modules.edocument_unedifact.edocument import (
    IncorrectValueForField, MissingFieldsError)
from trytond.modules.edocument_unedifact.edocument import (EdifactMixin,
    UOMS_EDI_TO_TRYTON, EdiTemplate)
from trytond.modules.edocument_unedifact.edocument import (Message, Serializer)
from trytond.modules.edocument_unedifact.edocument import (with_segment_check,
    separate_section, RewindIterator, DO_NOTHING, NO_ERRORS,
    validate_segment)
import os
from datetime import datetime
from itertools import chain
from decimal import Decimal


__all__ = ['Sale', 'SaleLine', 'Cron']

ZERO_ = Decimal('0')
NO_SALE = None
KNOWN_EXTENSIONS = ['.txt', '.edi', '.pla']
MODULE_PATH = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = 'ORDERS.yml'


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super(Cron, cls).__setup__()
        cls.method.selection.extend([
            ('sale.sale|get_sales_from_edi_files_cron', 'Create EDI Orders')])


class Sale(EdifactMixin, metaclass=PoolMeta):
    __name__ = 'sale.sale'

    def set_fields_value(self, values):
        """
        Set Sale fields values from a given dict
        """
        for field in self._fields.keys():
            value = values.get(field)
            if not value:
                default = self._defaults.get(field)
                if default:
                    value = default()
            setattr(self, field, value)
        return self

    @classmethod
    def import_edi_input(cls, response, template):
        """
        Creates a sale record from a given edi file
        :param edi_file: EDI file to be processed.
        :template_name: File name from the file used to validate the EDI msg.
        """

        pool = Pool()
        SaleLine = pool.get('sale.line')
        control_chars = cls.set_control_chars(
            template.get('control_chars', {}))
        message = Message.from_str(response.upper().replace('\r', ''),
            characters=control_chars)
        # If there isn't a segment UNH with ORDERS:D:96A:UN:EAN008
        # means the file readed it's not a EDI order.
        unh = message.get_segment('UNH')

        if (not unh or u"ORDERS:D:96A:UN:EAN008"
                not in Serializer().serialize([unh])):
            return NO_SALE, NO_ERRORS

        segments_iterator = RewindIterator(message.segments)
        header = [x for x in chain(*separate_section(segments_iterator,
                    start='BGM', end='LIN'))]
        detail = [x for x in separate_section(segments_iterator, start='LIN',
                end='UNS')]
        del(segments_iterator)

        total_errors = []
        discard_if_partial_sale = False
        values = {}
        nad_segments = []
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
            if segment.tag == 'NAD':
                nad_segments.append(segment)
                continue

            process = eval('cls._process_{}'.format(segment.tag))
            to_update, errors = process(segment, template_segment)
            if errors:
                total_errors += errors
                continue
            if to_update:
                if isinstance(to_update, dict):
                    for k, v in to_update.items():
                        if k in values.keys():
                            to_update = {k: "%s\n%s" % (values[k], v)}
                values.update(to_update)

        if not nad_segments:
            return NO_SALE, total_errors

        nad_results = {}
        template_segment = template['header'].get(u'NAD')
        for segment in nad_segments:
            result, errors = cls._process_NAD(segment, template_segment)
            if errors:
                total_errors += errors
            if result:
                nad_results.update(result)

        if nad_results:
            ms_parties = nad_results.get('MS', [])
            by_parties = nad_results.get('BY', [])
            dp_address = nad_results.get('DP')
            sale_parties = list(set(ms_parties + by_parties))
            address_party = dp_address.party if dp_address else None
            if address_party in sale_parties:
                values.update({
                    'shipment_party': address_party,
                    'shipment_address': dp_address,
                    })

        # If any header segment could be processed or there isn't a party
        # the sale isn't created
        if not values or not values.get('shipment_party'):
            return NO_SALE, total_errors

        sale = cls()
        sale.set_fields_value(values)
        sale.on_change_shipment_party()
        if not sale.party:
            sale.party = sale.shipment_party
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
                    if (len(segment.elements) > 0 and
                            len(segment.elements[0]) > 0 and
                            ((segment.elements[0][0] == 'INF' and
                                    values.get('unit_price', 0) != 0) or
                                (segment.elements[0][0] == 'AAA' and
                                    to_update['unit_price'] == 0 and
                                    values.get('unit_price')))):
                        continue
                    values.update(to_update)
            if errors:
                continue
            line = SaleLine().set_fields_value(values)
            # This fields are a required fields, we set its value to a
            # default valuein order to the sale can be saved. No matter
            # if it isn't the true value because it will be calculated next
            # in the on_change_product and on_change_quantity calls.
            if not getattr(line, 'description'):
                line.description = line.product.rec_name
            if not getattr(line, 'unit_price'):
                line.unit_price = ZERO_
            else:
                # If the line has a unit_price it means that isn't necessary
                # to apply discounts and it must be cleaned.
                for field in ('discount', 'discount1', 'discount2',
                        'discount3'):
                    if hasattr(line, field):
                        setattr(line, field, ZERO_)
            if hasattr(line, 'gross_unit_price') and not line.gross_unit_price:
                line.gross_unit_price = ZERO_
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
        if segment and segment.elements:
            element = segment.elements[3]
            if isinstance(element, list):
                element = "".join(element)
            return {'comment': element}, NO_ERRORS
        else:
            return DO_NOTHING, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_CTA(cls, segment, template):
        if segment and segment.elements:
            element = segment.elements[1]
            if isinstance(element, list):
                element = "".join(element)
            return {'comment': element}, NO_ERRORS
        else:
            return DO_NOTHING, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_COM(cls, segment, template):
        if segment and segment.elements:
            element = segment.elements[0][0]
            if isinstance(element, list):
                element = "".join(element)
            if segment.elements[0][2] == 'TE':
                element = "Telf: %s" % element
            elif segment.elements[0][2] == 'FX':
                element = "Fax: %s" % element
            elif segment.elements[0][2] == 'EM':
                element = "Email: %s" % element
            return {'comment': element}, NO_ERRORS
        else:
            return DO_NOTHING, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_NAD(cls, segment, template):
        serializer = Serializer()
        pool = Pool()
        PartyIdentifier = pool.get('party.identifier')
        Address = pool.get('party.address')
        if segment.elements[0] in ('MS', 'BY'):
            edi_operational_point = segment.elements[1][0]
            identifiers = PartyIdentifier.search([
                    ('type', '=', 'edi'),
                    ('code', 'ilike', edi_operational_point)])
            if not identifiers:
                serialized_segment = serializer.serialize([segment])
                msg = 'Party not found'
                return DO_NOTHING, ['{}: {}'.format(msg, serialized_segment)]
            return {'MS': [x.party for x in identifiers]}, NO_ERRORS
        elif segment.elements[0] == 'DP':
            edi_operational_point = segment.elements[1][0]
            address, = Address.search([
                    ('electronet_sale_point', 'ilike', edi_operational_point)
                    ], limit=1) or [None]
            if not address:
                serialized_segment = serializer.serialize([segment])
                msg = u'Addresses not found'
                return [], ['{}: {}'.format(msg, serialized_segment)]
            return {'DP': address}, NO_ERRORS

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
        qty_value = segment.elements[0][6]
        value = float(value)/float(qty_value)
        if segment.elements[0][0] in ('AAA', 'INF'):
            field = 'unit_price'
        elif segment.elements[0][0] == 'AAB':
            # If the model SaleLine doesn't have the field gross_unit_price
            # means the module sale_discount was not installed.
            if hasattr(SaleLine, 'gross_unit_price'):
                field = 'gross_unit_price'
        if not field:
            return DO_NOTHING, NO_ERRORS
        value = Decimal(value).quantize(Decimal(1) / 10 ** price_digits[1])
        return {field: value}, NO_ERRORS

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
    def create_edi_sales(cls, template=DEFAULT_TEMPLATE):
        """
        Get sales from edi files
        """
        pool = Pool()
        Configuration = pool.get('sale.configuration')
        configuration = Configuration(1)
        errors_path = os.path.abspath(configuration.edi_errors_path)
        source_path = os.path.abspath(configuration.edi_source_path)
        template_name = (configuration.template_sale_edi
            or DEFAULT_TEMPLATE)
        template_path = os.path.join(os.path.join(MODULE_PATH, 'templates'),
            template_name)
        template = EdiTemplate(template_name, template_path)
        return cls.process_edi_inputs(source_path, errors_path, template)

    @classmethod
    def apply_on_change_product_and_quantity_to_lines(cls, sales):
        pool = Pool()
        SaleLine = pool.get('sale.line')
        to_write = []
        for sale in sales:
            for line in sale.lines:
                line.apply_on_change_product_and_quantity()
            to_write.extend(sale.lines)
        if to_write:
            SaleLine.save(to_write)

    @classmethod
    def get_sales_from_edi_files(cls):
        '''Get orders from edi files'''
        results = cls.create_edi_sales()
        if results:
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
        for field in self._fields.keys():
            value = values.get(field)
            if not value:
                default = self._defaults.get(field)
                if default:
                    value = default()
            setattr(self, field, value)
        return self

    def apply_on_change_product_and_quantity(self):
        self.on_change_product()
        self.on_change_quantity()
