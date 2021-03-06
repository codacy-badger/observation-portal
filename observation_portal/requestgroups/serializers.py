import json
import logging
from json import JSONDecodeError

from rest_framework import serializers
from django.utils.translation import ugettext as _
from django.core.exceptions import ObjectDoesNotExist
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator

from observation_portal.proposals.models import TimeAllocation, Membership
from observation_portal.requestgroups.models import (
    Request, Target, Window, RequestGroup, Location, Configuration, Constraints, InstrumentConfig,
    AcquisitionConfig, GuidingConfig, RegionOfInterest
)
from observation_portal.requestgroups.models import DraftRequestGroup
from observation_portal.common.state_changes import debit_ipp_time, TimeAllocationError, validate_ipp
from observation_portal.requestgroups.target_helpers import TARGET_TYPE_HELPER_MAP
from observation_portal.common.configdb import configdb, ConfigDB, ConfigDBException
from observation_portal.requestgroups.duration_utils import (
    get_request_duration, get_request_duration_sum, get_total_duration_dict, OVERHEAD_ALLOWANCE,
    get_instrument_configuration_duration, get_num_exposures, get_semester_in
)
from datetime import timedelta
from observation_portal.common.rise_set_utils import get_filtered_rise_set_intervals_by_site, get_largest_interval

logger = logging.getLogger(__name__)


class ModeValidationHelper:
    """Class used to validate modes of different types"""
    def __init__(self, mode_type, instrument_type, default_modes, modes):
        self._mode_type = mode_type.lower()
        self._instrument_type = instrument_type
        self._default_modes = default_modes
        self._modes = modes
        self._mode_key = 'rotator_mode' if self._mode_type == 'rotator' else 'mode'
        self._modes_by_code = {}

    def _possible_modes(self) -> list:
        possible_modes = []
        if self._mode_type in self._default_modes:
            possible_modes.append(self._default_modes[self._mode_type])
        elif self._mode_type in self._modes:
            # There are modes to choose from. This would normally not happen since defaults should be set.
            possible_modes.extend(self._modes[self._mode_type]['modes'])
        return possible_modes

    def _unavailable_msg(self, config: dict) -> str:
        if self._mode_type in self._modes:
            if not config[self._mode_key].lower() in [m['code'].lower() for m in self._modes[self._mode_type]['modes']]:
                return (
                    f'{self._mode_type.capitalize()} mode {config[self._mode_key]} is not available for '
                    f'instrument type {self._instrument_type}'
                )
        return ''

    def _missing_fields_msg(self, config) -> str:
        missing_fields = []
        mode = configdb.get_mode_with_code(self._instrument_type, config[self._mode_key], self._mode_type)
        if 'required_fields' in mode.get('params', {}):
            for field in mode['params']['required_fields']:
                if 'extra_params' not in config or field not in config['extra_params']:
                    missing_fields.append(field)
        if missing_fields:
            return (
                f'{self._mode_type.capitalize()} Mode {mode["code"]} requires [{", ".join(missing_fields)}] '
                f'set in extra params'
            )
        return ''

    def mode_is_not_set(self, config: dict) -> bool:
        return self._mode_key not in config or not config[self._mode_key]

    def get_mode_to_set(self) -> dict:
        """Choose a mode to set"""
        mode = {'error': '', 'mode': {}}
        possible_modes = self._possible_modes()
        if len(possible_modes) == 1:
            # There is only one mode to choose from, so set that.
            mode['mode'] = possible_modes[0]
        elif len(possible_modes) > 1:
            # There are many possible modes, make the user choose.
            mode['error'] = (
                f'Must set a {self._mode_type} mode, choose '
                f'from {", ".join([mode["code"] for mode in self._modes["guiding"]["modes"]])}'
            )
        return mode

    def get_mode_error_msg(self, config: dict) -> str:
        """Return an error message if there is a problem with the mode"""
        if self._mode_type in self._modes:
            # Check if the mode exists
            unavailable_msg = self._unavailable_msg(config)
            if unavailable_msg:
                return unavailable_msg
            # Check if there are any required params that are not set
            missing_fields_msg = self._missing_fields_msg(config)
            if missing_fields_msg:
                return missing_fields_msg
        return ''


class CadenceSerializer(serializers.Serializer):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    period = serializers.FloatField(validators=[MinValueValidator(0.02)])
    jitter = serializers.FloatField(validators=[MinValueValidator(0.02)])

    def validate_end(self, value):
        if value < timezone.now():
            raise serializers.ValidationError('End time must be in the future')
        return value

    def validate(self, data):
        if data['start'] >= data['end']:
            msg = _("Cadence end '{}' cannot be earlier than cadence start '{}'.").format(data['start'], data['end'])
            raise serializers.ValidationError(msg)
        return data


class ConstraintsSerializer(serializers.ModelSerializer):
    max_airmass = serializers.FloatField(
        default=1.6, validators=[MinValueValidator(1.0), MaxValueValidator(25.0)]  # Duplicated in models.py
    )
    min_lunar_distance = serializers.FloatField(
        default=30.0, validators=[MinValueValidator(0.0), MaxValueValidator(180.0)]  # Duplicated in models.py
    )

    class Meta:
        model = Constraints
        exclude = Constraints.SERIALIZER_EXCLUDE


class RegionOfInterestSerializer(serializers.ModelSerializer):
    class Meta:
        model = RegionOfInterest
        exclude = RegionOfInterest.SERIALIZER_EXCLUDE

    def validate(self, data):
        return data


class InstrumentConfigSerializer(serializers.ModelSerializer):
    rois = RegionOfInterestSerializer(many=True, required=False)

    class Meta:
        model = InstrumentConfig
        exclude = InstrumentConfig.SERIALIZER_EXCLUDE

    def validate(self, data):
        if 'bin_x' in data and not 'bin_y' in data:
            data['bin_y'] = data['bin_x']
        elif 'bin_y' in data and not 'bin_x' in data:
            data['bin_x'] = data['bin_y']

        if 'bin_x' in data and 'bin_y' in data and data['bin_x'] != data['bin_y']:
            raise serializers.ValidationError(_(
                'Currently only square binnings are supported. Please submit with bin_x == bin_y'
            ))
        return data

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not data['rois']:
            del data['rois']
        return data


class AcquisitionConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AcquisitionConfig
        exclude = AcquisitionConfig.SERIALIZER_EXCLUDE

    def validate(self, data):
        return data


class GuidingConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = GuidingConfig
        exclude = GuidingConfig.SERIALIZER_EXCLUDE

    def validate(self, data):
        return data


class TargetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Target
        exclude = Target.SERIALIZER_EXCLUDE
        extra_kwargs = {
            'name': {'error_messages': {'blank': 'Please provide a name.'}}
        }

    def to_representation(self, instance):
        # Only return data for the specific target type
        data = super().to_representation(instance)
        target_helper = TARGET_TYPE_HELPER_MAP[data['type']](data)
        target_dict = {k: data.get(k) for k in target_helper.fields if data.get(k) is not None}
        target_dict['extra_params'] = data.get('extra_params', {})
        return target_dict

    def validate(self, data):
        target_helper = TARGET_TYPE_HELPER_MAP[data['type']](data)
        if target_helper.is_valid():
            data.update(target_helper.data)
        else:
            raise serializers.ValidationError(target_helper.error_dict)
        return data


class ConfigurationSerializer(serializers.ModelSerializer):
    fill_window = serializers.BooleanField(required=False, write_only=True)
    constraints = ConstraintsSerializer()
    instrument_configs = InstrumentConfigSerializer(many=True)
    acquisition_config = AcquisitionConfigSerializer()
    guiding_config = GuidingConfigSerializer()
    target = TargetSerializer()

    class Meta:
        model = Configuration
        exclude = Configuration.SERIALIZER_EXCLUDE
        read_only_fields = ('priority',)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Only return the repeat duration if its a REPEAT type configuration
        if 'REPEAT' not in data.get('type') and 'repeat_duration' in data:
            del data['repeat_duration']

        return data

    def validate_instrument_configs(self, value):
        if len(set([instrument_config.get('rotator_mode', '') for instrument_config in value])) > 1:
            raise serializers.ValidationError(_('Rotator modes within the same configuration must be the same'))
        if len(value) < 1:
            raise serializers.ValidationError(_('A configuration must have at least one instrument configuration'))
        return value

    def validate_instrument_type(self, value):
        is_staff = False
        request_context = self.context.get('request')
        if request_context:
            is_staff = request_context.user.is_staff
        if value and value not in configdb.get_instrument_types({}, only_schedulable=(not is_staff)):
            raise serializers.ValidationError(
                _('Invalid instrument type {}. Valid instruments may include: {}').format(
                    value, ', '.join(configdb.get_instrument_types({}, only_schedulable=(not is_staff)))
                )
            )
        return value

    def validate(self, data):
        # TODO: Validate the guiding optical elements on the guiding instrument types
        instrument_type = data['instrument_type']
        modes = configdb.get_modes_by_type(instrument_type)
        default_modes = configdb.get_default_modes_by_type(instrument_type)
        guiding_config = data['guiding_config']

        if len(data['instrument_configs']) > 1 and data['type'] in ['SCRIPT', 'SKY_FLAT']:
            raise serializers.ValidationError(_(f'Multiple instrument configs are not allowed for type {data["type"]}'))

        # Validate the guide mode
        guide_validation_helper = ModeValidationHelper('guiding', instrument_type, default_modes, modes)
        if guide_validation_helper.mode_is_not_set(guiding_config):
            guide_mode_to_set = guide_validation_helper.get_mode_to_set()
            if guide_mode_to_set['error']:
                raise serializers.ValidationError(_(guide_mode_to_set['error']))
            if guide_mode_to_set['mode']:
                guiding_config['mode'] = guide_mode_to_set['mode']['code']
            else:
                guiding_config['mode'] = GuidingConfig.OFF

        guide_mode_error_msg = guide_validation_helper.get_mode_error_msg(guiding_config)
        if guide_mode_error_msg:
            raise serializers.ValidationError(_(guide_mode_error_msg))

        if configdb.is_spectrograph(instrument_type) and data['type'] not in ['LAMP_FLAT', 'ARC', 'NRES_BIAS', 'NRES_DARK']:
            if 'optional' in guiding_config and guiding_config['optional']:
                raise serializers.ValidationError(_(
                    "Guiding cannot be optional on spectrograph instruments for types that are not ARC or LAMP_FLAT."
                ))
            guiding_config['optional'] = False

        if data['type'] in ['LAMP_FLAT', 'ARC', 'AUTO_FOCUS', 'NRES_BIAS', 'NRES_DARK', 'BIAS', 'DARK', 'SCRIPT']:
            # These types of observations should only ever be set to guiding mode OFF, but the acquisition modes for
            # spectrographs won't necessarily have that mode. Force OFF here.
            data['acquisition_config']['mode'] = AcquisitionConfig.OFF
        else:
            # Validate acquire modes
            acquisition_config = data['acquisition_config']
            acquire_validation_helper = ModeValidationHelper('acquisition', instrument_type, default_modes, modes)
            if acquire_validation_helper.mode_is_not_set(acquisition_config):
                acquire_mode_to_set = acquire_validation_helper.get_mode_to_set()
                if acquire_mode_to_set['error']:
                    raise serializers.ValidationError(_(acquire_mode_to_set['error']))
                if acquire_mode_to_set['mode']:
                    acquisition_config['mode'] = acquire_mode_to_set['mode']['code']
                else:
                    acquisition_config['mode'] = AcquisitionConfig.OFF

            acquire_mode_error_msg = acquire_validation_helper.get_mode_error_msg(acquisition_config)
            if acquire_mode_error_msg:
                raise serializers.ValidationError(_(acquire_mode_error_msg))

        available_optical_elements = configdb.get_optical_elements(instrument_type)
        for instrument_config in data['instrument_configs']:
            # Validate the readout mode and the binning. Readout modes and binning are tied
            # together- If one is set, we can determine the other.
            # TODO: Remove the binning checks when binnings are removed entirely
            readout_validation_helper = ModeValidationHelper('readout', instrument_type, default_modes, modes)
            if readout_validation_helper.mode_is_not_set(instrument_config):
                if 'bin_x' not in instrument_config and 'bin_y' not in instrument_config:
                    # Set the readout mode as well as the binning
                    readout_mode_to_set = readout_validation_helper.get_mode_to_set()
                    if readout_mode_to_set['error']:
                        raise serializers.ValidationError(_(readout_mode_to_set['error']))
                    if readout_mode_to_set['mode']:
                        instrument_config['mode'] = readout_mode_to_set['mode']['code']
                        instrument_config['bin_x'] = readout_mode_to_set['mode']['params']['binning']
                        instrument_config['bin_y'] = readout_mode_to_set['mode']['params']['binning']

                elif 'bin_x' in instrument_config:
                    # A binning is set already - figure out what the readout mode should be from that
                    try:
                        instrument_config['mode'] = configdb.get_readout_mode_with_binning(
                            instrument_type, instrument_config['bin_x']
                        )['code']
                    except ConfigDBException as cdbe:
                        raise serializers.ValidationError(_(str(cdbe)))
            else:
                # A readout mode is set - validate the mode
                readout_error_msg = readout_validation_helper.get_mode_error_msg(instrument_config)
                if readout_error_msg:
                    raise serializers.ValidationError(_(readout_error_msg))

                # At this point the readout mode that is set is valid. Now either set the binnings, or make
                # sure that those that are set are ok
                readout_mode = configdb.get_mode_with_code(instrument_type, instrument_config['mode'], 'readout')
                if 'bin_x' not in instrument_config:
                    instrument_config['bin_x'] = readout_mode['params']['binning']
                    instrument_config['bin_y'] = readout_mode['params']['binning']

                elif instrument_config['bin_x'] != readout_mode['params']['binning']:
                    raise serializers.ValidationError(_(
                        f'Binning {instrument_config["bin_x"]} is not a valid binning for readout mode '
                        f'{instrument_config["mode"]} for instrument type {instrument_type}'
                    ))

            # Validate the rotator modes
            if 'rotator' in modes:
                rotator_mode_validation_helper = ModeValidationHelper('rotator', instrument_type, default_modes, modes)
                if rotator_mode_validation_helper.mode_is_not_set(instrument_config):
                    rotator_mode_to_set = rotator_mode_validation_helper.get_mode_to_set()
                    if rotator_mode_to_set['error']:
                        raise serializers.ValidationError(_(rotator_mode_to_set['error']))
                    if rotator_mode_to_set['mode']:
                        instrument_config['rotator_mode'] = rotator_mode_to_set['mode']['code']

                rotator_error_msg = rotator_mode_validation_helper.get_mode_error_msg(instrument_config)
                if rotator_error_msg:
                    raise serializers.ValidationError(_(rotator_error_msg))

            # Check that the optical elements specified are valid in configdb
            for oe_type, value in instrument_config.get('optical_elements', {}).items():
                plural_type = '{}s'.format(oe_type)
                if plural_type not in available_optical_elements:
                    raise serializers.ValidationError(_("optical_element of type {} is not available on {} instruments"
                                                        .format(oe_type, data['instrument_type'])))
                available_elements = {element['code'].lower(): element['code'] for element in available_optical_elements[plural_type]}
                if plural_type in available_optical_elements and value.lower() not in available_elements.keys():
                    raise serializers.ValidationError(_("optical element {} of type {} is not available".format(
                        value, oe_type
                    )))
                else:
                    instrument_config['optical_elements'][oe_type] = available_elements[value.lower()]

            # Also check that any optical element group in configdb is specified in the request unless we are a BIAS or
            # DARK or SCRIPT type observation
            observation_types_without_oe = ['BIAS', 'DARK', 'SCRIPT']
            if data['type'].upper() not in observation_types_without_oe:
                for oe_type in available_optical_elements.keys():
                    singular_type = oe_type[:-1] if oe_type.endswith('s') else oe_type
                    if singular_type not in instrument_config.get('optical_elements', {}):
                        raise serializers.ValidationError(_(
                            f'Must set optical element of type {singular_type} for instrument type {instrument_type}'
                        ))
            # Validate any regions of interest
            if 'rois' in instrument_config:
                max_rois = configdb.get_max_rois(instrument_type)
                ccd_size = configdb.get_ccd_size(instrument_type)
                if len(instrument_config['rois']) > max_rois:
                    raise serializers.ValidationError(_(
                        f'Instrument type {instrument_type} supports up to {max_rois} regions of interest'
                    ))
                for roi in instrument_config['rois']:
                    if 'x1' not in roi and 'x2' not in roi and 'y1' not in roi and 'y2' not in roi:
                        raise serializers.ValidationError(_('Must submit at least one bound for a region of interest'))

                    if 'x1' not in roi:
                        roi['x1'] = 0
                    if 'x2' not in roi:
                        roi['x2'] = ccd_size['x']
                    if 'y1' not in roi:
                        roi['y1'] = 0
                    if 'y2' not in roi:
                        roi['y2'] = ccd_size['y']

                    if roi['x1'] >= roi['x2'] or roi['y1'] >= roi['y2']:
                        raise serializers.ValidationError(_(
                            'Region of interest pixels start must be less than pixels end'
                        ))

                    if roi['x2'] > ccd_size['x'] or roi['y2'] > ccd_size['y']:
                        raise serializers.ValidationError(_(
                            'Regions of interest for instrument type {} must be in range 0<=x<={} and 0<=y<={}'.format(
                                instrument_type, ccd_size['x'], ccd_size['y']
                            ))
                        )

        if data['type'] == 'SCRIPT':
            if (
                    'extra_params' not in data
                    or 'script_name' not in data['extra_params']
                    or not data['extra_params']['script_name']
            ):
                raise serializers.ValidationError(_(
                    'Must specify a script_name in extra_params for SCRIPT configuration type'
                ))

        # Validate duration is set if it's a REPEAT_* type configuration
        if 'REPEAT' in data['type']:
            if 'repeat_duration' not in data or data['repeat_duration'] is None:
                raise serializers.ValidationError(_(
                    f'Must specify a configuration repeat_duration for {data["type"]} type configurations.'
                ))
            else:
                # Validate that the duration exceeds the minimum to run everything at least once
                min_duration = sum(
                    [get_instrument_configuration_duration(
                        ic, data['instrument_type']) for ic in data['instrument_configs']]
                )
                if min_duration > data['repeat_duration']:
                    raise serializers.ValidationError(_(
                        f'Configuration repeat_duration of {data["repeat_duration"]} is less than the minimum of '
                        f'{min_duration} required to repeat at least once'
                    ))
        else:
            if 'repeat_duration' in data and data['repeat_duration'] is not None:
                raise serializers.ValidationError(_(
                    'You may only specify a repeat_duration for REPEAT_* type configurations.'
                ))

        # Validate the configuration type is available for the instrument requested
        if data['type'] not in configdb.get_configuration_types(instrument_type):
            raise serializers.ValidationError(_(
                f'configuration type {data["type"]} is not valid for instrument type {instrument_type}'
            ))
        return data


class LocationSerializer(serializers.ModelSerializer):
    site = serializers.ChoiceField(choices=configdb.get_site_tuples(), required=False)
    enclosure = serializers.ChoiceField(choices=configdb.get_enclosure_tuples(), required=False)
    telescope = serializers.ChoiceField(choices=configdb.get_telescope_tuples(), required=False)
    telescope_class = serializers.ChoiceField(choices=configdb.get_telescope_class_tuples(), required=True)

    class Meta:
        model = Location
        exclude = Location.SERIALIZER_EXCLUDE

    def validate(self, data):
        if 'enclosure' in data and 'site' not in data:
            raise serializers.ValidationError(_("Must specify a site with an enclosure."))
        if 'telescope' in data and 'enclosure' not in data:
            raise serializers.ValidationError(_("Must specify an enclosure with a telescope."))

        site_data_dict = {site['code']: site for site in configdb.get_site_data()}
        if 'site' in data:
            if data['site'] not in site_data_dict:
                msg = _('Site {} not valid. Valid choices: {}').format(data['site'], ', '.join(site_data_dict.keys()))
                raise serializers.ValidationError(msg)
            enc_set = site_data_dict[data['site']]['enclosure_set']
            enc_dict = {enc['code']: enc for enc in enc_set}
            if 'enclosure' in data:
                if data['enclosure'] not in enc_dict:
                    raise serializers.ValidationError(_(
                        f'Enclosure {data["enclosure"]} not valid. Valid choices: {", ".join(enc_dict.keys())}'
                    ))
                tel_set = enc_dict[data['enclosure']]['telescope_set']
                tel_list = [tel['code'] for tel in tel_set]
                if 'telescope' in data and data['telescope'] not in tel_list:
                    msg = _('Telescope {} not valid. Valid choices: {}').format(data['telescope'], ', '.join(tel_list))
                    raise serializers.ValidationError(msg)

        return data

    def to_representation(self, instance):
        """
        This method is overridden to remove blank fields from serialized output. We could put this into a subclassed
        ModelSerializer if we want it to apply to all our Serializers.
        """
        rep = super().to_representation(instance)
        return {key: val for key, val in rep.items() if val}


class WindowSerializer(serializers.ModelSerializer):
    start = serializers.DateTimeField(required=False)

    class Meta:
        model = Window
        exclude = Window.SERIALIZER_EXCLUDE

    def validate(self, data):
        if 'start' not in data:
            data['start'] = timezone.now()
        if data['end'] <= data['start']:
            msg = _(f"Window end '{data['end']}' cannot be earlier than window start '{data['start']}'")
            raise serializers.ValidationError(msg)

        if not get_semester_in(data['start'], data['end']):
            raise serializers.ValidationError('The observation window does not fit within any defined semester.')
        return data

    def validate_end(self, value):
        if value < timezone.now():
            raise serializers.ValidationError('Window end time must be in the future')
        return value


class RequestSerializer(serializers.ModelSerializer):
    location = LocationSerializer()
    configurations = ConfigurationSerializer(many=True)
    windows = WindowSerializer(many=True)
    cadence = CadenceSerializer(required=False, write_only=True)
    duration = serializers.ReadOnlyField()

    class Meta:
        model = Request
        read_only_fields = (
            'id', 'created', 'duration', 'state',
        )
        exclude = Request.SERIALIZER_EXCLUDE

    def validate_configurations(self, value):
        if not value:
            raise serializers.ValidationError(_('You must specify at least 1 configuration'))

        # Only one configuration can have the fill_window attribute set
        if [config.get('fill_window', False) for config in value].count(True) > 1:
            raise serializers.ValidationError(_('Only one configuration can have `fill_window` set'))

        constraints = value[0]['constraints']
        # Set the relative priority of molecules in order
        for i, configuration in enumerate(value):
            configuration['priority'] = i + 1
            if configuration['constraints'] != constraints:
                raise serializers.ValidationError(_(
                    'Currently only a single constraints per Request is supported. This restriction will be '
                    'lifted in the future.'
                ))
        return value

    def validate_windows(self, value):
        if not value:
            raise serializers.ValidationError(_('You must specify at least 1 window'))

        if len(set([get_semester_in(window['start'], window['end']) for window in value])) > 1:
            raise serializers.ValidationError(_('The observation windows must all be in the same semester'))

        return value

    def validate_cadence(self, value):
        if value:
            raise serializers.ValidationError(_('Please use the cadence endpoint to expand your cadence request'))
        return value

    def validate(self, data):
        is_staff = False
        only_schedulable = True
        request_context = self.context.get('request')
        if request_context:
            is_staff = request_context.user.is_staff
            only_schedulable = not (is_staff and ConfigDB.is_location_fully_set(data.get('location', {})))
        # check if the instrument specified is allowed
        # TODO: Check if ALL instruments are available at a resource defined by location
        if 'location' in data:
            # Check if the location is fully specified, and if not then use only schedulable instruments
            valid_instruments = configdb.get_instrument_types(data.get('location', {}),
                                                              only_schedulable=only_schedulable)
            for configuration in data['configurations']:
                if configuration['instrument_type'] not in valid_instruments:
                    msg = _("Invalid instrument type '{}' at site={}, enc={}, tel={}. \n").format(
                        configuration['instrument_type'],
                        data.get('location', {}).get('site', 'Any'),
                        data.get('location', {}).get('enclosure', 'Any'),
                        data.get('location', {}).get('telescope', 'Any')
                    )
                    msg += _("Valid instruments include: ")
                    for inst_name in valid_instruments:
                        msg += inst_name + ', '
                    msg += '.'
                    if is_staff and not only_schedulable:
                        msg += '\nStaff users must fully specify location to schedule on non-SCHEDULABLE instruments'
                    raise serializers.ValidationError(msg)

        if 'acceptability_threshold' not in data:
            data['acceptability_threshold'] = max(
                [configdb.get_default_acceptability_threshold(configuration['instrument_type'])
                 for configuration in data['configurations']]
            )

        # check that the requests window has enough rise_set visible time to accomodate the requests duration
        if data.get('windows'):
            duration = get_request_duration(data)
            rise_set_intervals_by_site = get_filtered_rise_set_intervals_by_site(data, is_staff=is_staff)
            largest_interval = get_largest_interval(rise_set_intervals_by_site)
            for configuration in data['configurations']:
                if 'REPEAT' in configuration['type'].upper() and configuration.get('fill_window'):
                    max_configuration_duration = largest_interval.total_seconds() - duration + configuration.get('repeat_duration', 0) - 1
                    configuration['repeat_duration'] = max_configuration_duration
                    duration = get_request_duration(data)

                # delete the fill window attribute, it is only used for this validation
                try:
                    del configuration['fill_window']
                except KeyError:
                    pass
            if largest_interval.total_seconds() <= 0:
                raise serializers.ValidationError(
                    _(
                        'According to the constraints of the request, the target is never visible within the time '
                        'window. Check that the target is in the nighttime sky. Consider modifying the time '
                        'window or loosening the airmass or lunar separation constraints. If the target is '
                        'non sidereal, double check that the provided elements are correct.'
                    )
                )
            if largest_interval.total_seconds() <= duration:
                raise serializers.ValidationError(
                    (
                        'According to the constraints of the request, the target is visible for a maximum of {0:.2f} '
                        'hours within the time window. This is less than the duration of your request {1:.2f} hours. '
                        'Consider expanding the time window or loosening the airmass or lunar separation constraints.'
                    ).format(
                        largest_interval.total_seconds() / 3600.0,
                        duration / 3600.0
                    )
                )
        return data


class CadenceRequestSerializer(RequestSerializer):
    cadence = CadenceSerializer()
    windows = WindowSerializer(required=False, many=True)

    def validate_cadence(self, value):
        return value

    def validate_windows(self, value):
        if value:
            raise serializers.ValidationError(_('Cadence requests may not contain windows'))

        return value


class RequestGroupSerializer(serializers.ModelSerializer):
    requests = RequestSerializer(many=True)
    submitter = serializers.StringRelatedField(default=serializers.CurrentUserDefault(), read_only=True)
    submitter_id = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = RequestGroup
        fields = '__all__'
        read_only_fields = (
            'id', 'created', 'state', 'modified'
        )
        extra_kwargs = {
            'proposal': {'error_messages': {'null': 'Please provide a proposal.'}},
            'name': {'error_messages': {'blank': 'Please provide a name.'}}
        }

    def create(self, validated_data):
        request_data = validated_data.pop('requests')

        with transaction.atomic():
            request_group = RequestGroup.objects.create(**validated_data)

            for r in request_data:
                configurations_data = r.pop('configurations')

                location_data = r.pop('location', {})
                windows_data = r.pop('windows', [])
                request = Request.objects.create(request_group=request_group, **r)

                if validated_data['observation_type'] != RequestGroup.DIRECT:
                    Location.objects.create(request=request, **location_data)
                    for window_data in windows_data:
                        Window.objects.create(request=request, **window_data)

                for configuration_data in configurations_data:
                    instrument_configs_data = configuration_data.pop('instrument_configs')
                    acquisition_config_data = configuration_data.pop('acquisition_config')
                    guiding_config_data = configuration_data.pop('guiding_config')
                    target_data = configuration_data.pop('target')
                    constraints_data = configuration_data.pop('constraints')
                    configuration = Configuration.objects.create(request=request, **configuration_data)

                    AcquisitionConfig.objects.create(configuration=configuration, **acquisition_config_data)
                    GuidingConfig.objects.create(configuration=configuration, **guiding_config_data)
                    Target.objects.create(configuration=configuration, **target_data)
                    Constraints.objects.create(configuration=configuration, **constraints_data)

                    for instrument_config_data in instrument_configs_data:
                        rois_data = []
                        if 'rois' in instrument_config_data:
                            rois_data = instrument_config_data.pop('rois')
                        instrument_config = InstrumentConfig.objects.create(configuration=configuration,
                                                                            **instrument_config_data)
                        for roi_data in rois_data:
                            RegionOfInterest.objects.create(instrument_config=instrument_config, **roi_data)

        if validated_data['observation_type'] == RequestGroup.NORMAL:
            debit_ipp_time(request_group)

        logger.info('RequestGroup created', extra={'tags': {
            'user': request_group.submitter.username,
            'tracking_num': request_group.id,
            'name': request_group.name
        }})
        cache.set('observation_portal_last_change_time', timezone.now(), None)

        return request_group

    def validate(self, data):
        # check that the user belongs to the supplied proposal
        user = self.context['request'].user
        if data['proposal'] not in user.proposal_set.all():
            raise serializers.ValidationError(
                _('You do not belong to the proposal you are trying to submit')
            )

        # validation on the operator matching the number of requests
        if data['operator'] == 'SINGLE':
            if len(data['requests']) > 1:
                raise serializers.ValidationError(
                    _("'Single' type requestgroups must have exactly one child request.")
                )
        elif len(data['requests']) == 1:
            raise serializers.ValidationError(
                _("'{}' type requestgroups must have more than one child request.".format(data['operator'].title()))
            )

        # Check that the user has not exceeded the time limit on this membership
        membership = Membership.objects.get(user=user, proposal=data['proposal'])
        if membership.time_limit >= 0:
            duration = sum(d for i, d in get_request_duration_sum(data).items())
            time_to_be_used = user.profile.time_used_in_proposal(data['proposal']) + duration
            if membership.time_limit < time_to_be_used:
                raise serializers.ValidationError(
                    _('This request\'s duration will exceed the time limit set for your account on this proposal.')
                )

        if data['observation_type'] == RequestGroup.DIRECT:
            # Don't do any time accounting stuff if it is a directly scheduled observation
            return data
        else:
            for request in data['requests']:
                target = request['configurations'][0]['target']
                for config in request['configurations']:
                    # for non-DIRECT observations, don't allow HOUR_ANGLE targets
                    if config['target']['type'] == 'HOUR_ANGLE':
                        raise serializers.ValidationError(_('HOUR_ANGLE Target type not supported in scheduled observations'))

                    # For non-DIRECT observations, only allow a single target
                    # TODO: Remove this check once we support multiple targets/constraints
                    if config['target'] != target:
                        raise serializers.ValidationError(_(
                            'Currently only a single target per Request is supported. This restriction will be lifted '
                            'in the future.'
                        ))
        try:
            total_duration_dict = get_total_duration_dict(data)
            for tak, duration in total_duration_dict.items():
                time_allocation = TimeAllocation.objects.get(
                    semester=tak.semester,
                    instrument_type=tak.instrument_type,
                    proposal=data['proposal'],
                )
                time_available = 0
                if data['observation_type'] == RequestGroup.NORMAL:
                    time_available = time_allocation.std_allocation - time_allocation.std_time_used
                elif data['observation_type'] == RequestGroup.RAPID_RESPONSE:
                    time_available = time_allocation.rr_allocation - time_allocation.rr_time_used
                    # For Rapid Response observations, check if the end time of the window is within
                    # 24 hours + the duration of the observation
                    for request in data['requests']:
                        windows = request.get('windows')
                        for window in windows:
                            if window.get('start') - timezone.now() > timedelta(seconds=0):
                                raise serializers.ValidationError(
                                    _("The Rapid Response observation window start time cannot be in the future.")
                                )
                            if window.get('end') - timezone.now() > timedelta(seconds=(duration + 86400)):
                                raise serializers.ValidationError(
                                    _(
                                        "A Rapid Response observation must start within the next 24 hours, so the "
                                        "window end time must be within the next (24 hours + the observation duration)"
                                    )
                                )
                elif data['observation_type'] == RequestGroup.TIME_CRITICAL:
                    # Time critical time
                    time_available = time_allocation.tc_allocation - time_allocation.tc_time_used

                if time_available <= 0.0:
                    raise serializers.ValidationError(
                        _("Proposal {} does not have any {} time left allocated in semester {} on {} instruments").format(
                            data['proposal'], data['observation_type'], tak.semester, tak.instrument_type)
                    )
                elif time_available * OVERHEAD_ALLOWANCE < (duration / 3600.0):
                    raise serializers.ValidationError(
                        _("Proposal {} does not have enough {} time allocated in semester {}").format(
                            data['proposal'], data['observation_type'], tak.semester)
                    )
            # validate the ipp debitting that will take place later
            if data['observation_type'] == RequestGroup.NORMAL:
                validate_ipp(data, total_duration_dict)
        except ObjectDoesNotExist:
            raise serializers.ValidationError(
                _("You do not have sufficient {} time allocated on the instrument you're requesting for this proposal.".format(
                    data['observation_type']
                ))
            )
        except TimeAllocationError as e:
            raise serializers.ValidationError(repr(e))

        return data

    def validate_requests(self, value):
        if not value:
            raise serializers.ValidationError(_('You must specify at least 1 request'))
        return value


class DraftRequestGroupSerializer(serializers.ModelSerializer):
    author = serializers.SlugRelatedField(
        read_only=True,
        slug_field='username',
        default=serializers.CurrentUserDefault()
    )

    class Meta:
        model = DraftRequestGroup
        fields = '__all__'
        read_only_fields = ('author',)

    def validate(self, data):
        if data['proposal'] not in self.context['request'].user.proposal_set.all():
            raise serializers.ValidationError('You are not a member of that proposal')
        return data

    def validate_content(self, data):
        try:
            json.loads(data)
        except JSONDecodeError:
            raise serializers.ValidationError('Content must be valid JSON')
        return data
