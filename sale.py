# -*- coding: utf-8 -*
# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool, PoolMeta
from trytond.model import fields
from edifact.message import Message
from edifact.serializer import Serializer
from edifact.utils import RewindIterator, rewind
import oyaml as yaml
from io import open
import os
from decorator import decorator
from datetime import datetime


__all__ = ['Sale', 'SaleLine']

MODULE_PATH = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = 'templates/ORDERS.yml'
NO_ERRORS = None
DO_NOTHING = {}
UOMS_EDI_TO_TRYTON = {
    'KGM': 'kg',
    'LTR': 'l',
    'MTR': 'm',
    'UN': 'u',
}


class EdiError(Exception):
    pass


class MissingFieldsError(EdiError):
    pass


class IncorrectValueForField(EdiError):
    pass


@decorator
def with_segment_check(func, *args):
    """
    This decorator provides a call to the validation of the segment struc
    against the template.
    """
    try:
        args = list(args)
        serializer = Serializer()
        cls = args.pop(0)
        segment = args.pop(0)
        template = args.pop(0)
        cls._validate_segment(segment.elements, template)
    except MissingFieldsError:
        serialized_segment = serializer.serialize([segment])
        msg = u'Some field is missing in segment'
        return DO_NOTHING, [u'{}: {}'.format(msg, serialized_segment)]
    except IncorrectValueForField:
        serialized_segment = serializer.serialize([segment])
        msg = 'Incorrect value for field in segment'
        return DO_NOTHING, [u'{}: {}'.format(msg, unicode(serialized_segment))]
    else:
        return func(cls, segment, template)


class Sale:
    __name__ = 'sale.sale'
    __metaclass__ = PoolMeta

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
        message = Message.from_str(edi_file)
        segments_iterator = RewindIterator(message.segments)

        header = [x for x in cls._separate_header(segments_iterator)]
        detail = [x for x in cls._separate_detail(segments_iterator)]
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
            if segment.tag == u'ALI':
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
            return None, total_errors

        sale = cls()
        for k, v in values.iteritems():
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
                process = eval('cls._process_{}'.format(segment.tag))
                to_update, errors = process(segment, template_segment)
                if errors:
                    # If there are errors the linegroup isn't processed
                    total_errors += errors
                    break
                if to_update:
                    values.update(to_update)
            if errors:
                continue
            values.update({'sale': sale.id})
            line = SaleLine().set_fields_value(values)
            line.on_change_product()
            line.on_change_quantity()
            lines.append(line)

        sale.lines = lines
        return sale, total_errors

    @classmethod
    def _validate_segment(cls, elements, template_segment_elements):
        """
        Validate the segment elements against the template
        """
        if len(template_segment_elements) > len(elements):
            raise MissingFieldsError
        for index, item in enumerate(template_segment_elements):
            if item == u'!ignore':
                continue
            elif item == u'!value':
                if not elements[index]:
                    raise IncorrectValueForField
            elif isinstance(item, list):
                # Recursively checks childs
                cls._validate_segment(elements[index], item)
            elif isinstance(item, tuple):
                if elements[index] not in item:
                    raise IncorrectValueForField
            else:
                if elements[index] != item:
                    raise IncorrectValueForField

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
        if segment.elements[0] in ('DP', 'BY'):
            edi_operational_point = segment.elements[1][0]
            identifier = PartyIdentifier.search([
                    ('type', '=', 'edi'),
                    ('code', '=', edi_operational_point)],
                limit=1)
            if not identifier:
                serialized_segment = serializer.serialize([segment])
                msg = u'Party not found'
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
            msg = u'Currency not found'
            return DO_NOTHING, ['{}: {}'.format(msg, serialized_segment)]
        currency, = currency
        return {'currency': currency}, NO_ERRORS

    @classmethod
    def _process_PIA(cls, segment, template):
        pool = Pool()
        Product = pool.get('product.product')
        try:
            cls._validate_segment(segment.elements, template)
        except MissingFieldsError:
            return DO_NOTHING, NO_ERRORS
        except IncorrectValueForField:
            serializer = Serializer()
            serialized_segment = serializer.serialize([segment])
            msg = 'Incorrect value for field in segment'
            return DO_NOTHING, [u'{}: {}'.format(
                    msg,
                    unicode(serialized_segment))]
        else:
            code = segment.elements[1][0]
            product = Product.search([('code', '=', code)], limit=1)
            if not product:
                serializer = Serializer()
                serialized_segment = serializer.serialize([segment])
                msg = 'No product found in segment'
                return DO_NOTHING, [u'{}: {}'.format(
                    msg,
                    unicode(serialized_segment))]
            return {'product': product[0].id}, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_QTY(cls, segment, template):
        pool = Pool()
        Uom = pool.get('product.uom')
        uom_value = UOMS_EDI_TO_TRYTON.get(segment.elements[0][-1], u'u')
        uom, = Uom.search([('symbol', '=', uom_value)], limit=1)
        quantity = float(segment.elements[0][2])
        return {'unit': uom, 'quantity': quantity}, NO_ERRORS

    @classmethod
    @with_segment_check
    def _process_DTM(cls, segment, template):
        date = datetime.strptime(segment.elements[0][2], '%Y%m%d')
        return {'shipping_date': date}, NO_ERRORS

    @classmethod
    def _separate_header(cls, iterator):
        """
        Extracts the header from the rest of the message
        """
        for segment in iterator:
            if segment.tag == 'LIN':
                rewind(iterator)
                break
            else:
                yield segment

    @classmethod
    def _separate_detail(cls, iterator):
        """
        Extracts the detail from the rest of the message
        """
        line = []
        for segment in iterator:
            if segment.tag == 'LIN':
                if line:
                    yield line
                line = []
                line.append(segment)
            elif segment.tag != 'UNS':
                if line:
                    line.append(segment)
            else:
                yield line
                rewind(iterator)
                break

    @classmethod
    def get_sales_from_edi_files(cls, template=DEFAULT_TEMPLATE):
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
            if fname[-4:] != '.txt':
                continue
            with open(fname, 'rb') as fp:
                edi_file = fp.read()
            try:
                sale, errors = cls.create_sale_from_edi_file(
                    edi_file.encode('utf-8'), template)
            except RuntimeError:
                continue
            else:
                if sale:
                    sale.edi_order_file = edi_file
                    sales.append(sale)
                    to_delete.append(fname)
                if errors:
                    error_fname = os.path.join(errors_path, 'error_{}.EDI'.format(
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
    def get_sales_from_edi_files_cron(cls):
        """
        Cron get orders from edi files:
        - State: active
        """
        cls.get_sales_from_edi_files()
        return True


class SaleLine:
    __name__ = 'sale.line'
    __metaclass__ = PoolMeta

    def set_fields_value(self, values):
        """
        Set SaleLine fields values from a given dict
        """
        for k, v in values.iteritems():
            setattr(self, k, v)
        return self
