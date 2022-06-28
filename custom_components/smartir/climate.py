import asyncio
import json
import logging
import os.path
import math
import time

import voluptuous as vol

from homeassistant.components import switch
from homeassistant.components.climate import ClimateEntity, PLATFORM_SCHEMA
from homeassistant.components.climate.const import (
    HVAC_MODE_OFF, HVAC_MODE_HEAT, HVAC_MODE_COOL,
    HVAC_MODE_DRY, HVAC_MODE_FAN_ONLY, HVAC_MODE_AUTO,
    SUPPORT_TARGET_TEMPERATURE, SUPPORT_TARGET_HUMIDITY, SUPPORT_FAN_MODE,
    SUPPORT_SWING_MODE, HVAC_MODES, ATTR_HVAC_MODE, ATTR_HUMIDITY)
from homeassistant.const import (
    CONF_NAME, STATE_ON, STATE_OFF, STATE_UNKNOWN, STATE_UNAVAILABLE,
    ATTR_TEMPERATURE, ATTR_DEVICE_CLASS, ATTR_ENTITY_ID,
    SERVICE_TURN_ON, SERVICE_TURN_OFF,
    PRECISION_TENTHS, PRECISION_HALVES, PRECISION_WHOLE)
from homeassistant.core import callback, DOMAIN as HA_DOMAIN
from homeassistant.helpers.event import (
    async_track_state_change,
    async_call_later,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from . import (
    COMPONENT_ABS_DIR, Helper,
    CONF_UNIQUE_ID, CONF_DEVICE_CODE, CONF_CONTROLLER, CONF_CONTROLLER_TYPE, CONF_CONTROLLER_DATA,
    CONF_DELAY, CONF_TEMPERATURE_SENSOR, CONF_HUMIDITY_SENSOR, CONF_POWER_SENSOR, CONF_POWER_SENSOR_RESTORE_STATE
)
from .controllers import get_controller

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Climate"
DEFAULT_DELAY = 0.5
DEFAULT_COLD_TOLERANCE = 0.5
DEFAULT_HOT_TOLERANCE = 0.3
DEFAULT_DELAY_ON = 2    # seconds
DEFAULT_DELAY_OFF = 120 # seconds, 2min
DEFAULT_RUN_TIME = 1800 # seconds,30min

CONF_USE_TEMPERATURE_SENSOR = "use_temperature_sensor"
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_POWER_METER_SENSOR = "power_meter_sensor"
CONF_DELAY_ON = "delay_on"
CONF_DELAY_OFF = "delay_off"
CONF_OFF_POWER_METER = "off_power_meter"
CONF_MIN_POWER_METER = "min_power_meter"
CONF_MAX_POWER_METER = "max_power_meter"
CONF_RUN_TIME = "run_time"

SUPPORT_FLAGS = (
    SUPPORT_TARGET_TEMPERATURE |
    SUPPORT_FAN_MODE
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_DEVICE_CODE): cv.positive_int,
    vol.Required(CONF_CONTROLLER_DATA): cv.string,
    vol.Optional(CONF_CONTROLLER): cv.string,
    vol.Optional(CONF_CONTROLLER_TYPE): cv.string,
    vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.positive_float,
    vol.Optional(CONF_TEMPERATURE_SENSOR): cv.entity_id,
    vol.Optional(CONF_HUMIDITY_SENSOR): cv.entity_id,
    vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
    vol.Optional(CONF_POWER_SENSOR_RESTORE_STATE, default=False): cv.boolean,
    vol.Optional(CONF_USE_TEMPERATURE_SENSOR): cv.boolean,
    vol.Optional(CONF_COLD_TOLERANCE, default=DEFAULT_COLD_TOLERANCE): cv.positive_float,
    vol.Optional(CONF_HOT_TOLERANCE, default=DEFAULT_HOT_TOLERANCE): cv.positive_float,
    vol.Optional(CONF_POWER_METER_SENSOR): cv.entity_id,
    vol.Optional(CONF_DELAY_ON, default=DEFAULT_DELAY_ON): cv.positive_time_period,
    vol.Optional(CONF_DELAY_OFF, default=DEFAULT_DELAY_OFF): cv.positive_time_period,
    vol.Optional(CONF_OFF_POWER_METER): cv.positive_float,
    vol.Optional(CONF_MIN_POWER_METER): cv.positive_float,
    vol.Optional(CONF_MAX_POWER_METER): cv.positive_float,
    vol.Optional(CONF_RUN_TIME, default=DEFAULT_RUN_TIME): cv.positive_time_period,
})

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the IR Climate platform."""
    device_code = config.get(CONF_DEVICE_CODE)
    device_files_subdir = os.path.join('codes', 'climate')
    device_files_absdir = os.path.join(COMPONENT_ABS_DIR, device_files_subdir)

    if not os.path.isdir(device_files_absdir):
        os.makedirs(device_files_absdir)

    device_json_filename = str(device_code) + '.json'
    device_json_path = os.path.join(device_files_absdir, device_json_filename)

    if not os.path.exists(device_json_path):
        _LOGGER.warning("Couldn't find the device Json file. The component will " \
                        "try to download it from the GitHub repo.")

        try:
            codes_source = ("https://raw.githubusercontent.com/"
                            "smartHomeHub/SmartIR/master/"
                            "codes/climate/{}.json")

            await Helper.downloader(codes_source.format(device_code), device_json_path)
        except Exception:
            _LOGGER.error("There was an error while downloading the device Json file. " \
                          "Please check your internet connection or if the device code " \
                          "exists on GitHub. If the problem still exists please " \
                          "place the file manually in the proper directory.")
            return

    with open(device_json_path) as j:
        try:
            device_data = json.load(j)
        except Exception:
            _LOGGER.error("The device Json file is invalid")
            return

    async_add_entities([SmartIRClimate(
        hass, config, device_data
    )])

class SmartIRClimate(ClimateEntity, RestoreEntity):
    def __init__(self, hass, config, device_data):
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        controller = config.get(CONF_CONTROLLER)
        self._controller_type = config.get(CONF_CONTROLLER_TYPE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._temperature_sensor = config.get(CONF_TEMPERATURE_SENSOR)
        self._humidity_sensor = config.get(CONF_HUMIDITY_SENSOR)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._power_sensor_restore_state = config.get(CONF_POWER_SENSOR_RESTORE_STATE)
        self._use_temperature_sensor = config.get(CONF_USE_TEMPERATURE_SENSOR)
        self._cold_tolerance = config.get(CONF_COLD_TOLERANCE)
        self._hot_tolerance = config.get(CONF_HOT_TOLERANCE)
        self._power_meter_sensor = config.get(CONF_POWER_METER_SENSOR)
        self._delay_on = config.get(CONF_DELAY_ON)
        self._delay_off = config.get(CONF_DELAY_OFF)
        self._off_power_meter = config.get(CONF_OFF_POWER_METER)
        self._min_power_meter = config.get(CONF_MIN_POWER_METER)
        self._max_power_meter = config.get(CONF_MAX_POWER_METER)
        self._run_time = config.get(CONF_RUN_TIME)

        self._manufacturer = device_data['manufacturer']
        self._supported_models = device_data['supportedModels']
        self._supported_controller = device_data['supportedController']
        self._commands_encoding = device_data['commandsEncoding']
        self._min_temperature = device_data['minTemperature']
        self._max_temperature = device_data['maxTemperature']
        self._min_humidity = device_data.get('minHumidity') or 30
        self._max_humidity = device_data.get('maxHumidity') or 99
        self._precision = device_data['precision']

        valid_hvac_modes = [x for x in device_data['operationModes'] if x in HVAC_MODES]

        self._operation_modes = [HVAC_MODE_OFF] + valid_hvac_modes
        self._fan_modes = device_data['fanModes']
        self._swing_modes = device_data.get('swingModes')
        self._commands = device_data['commands']

        self._target_temperature = self._min_temperature
        # the target temperature on climate
        self._target_temperature_climate = self._min_temperature
        self._target_humidity = self._min_humidity
        self._hvac_mode = HVAC_MODE_OFF
        self._current_fan_mode = self._fan_modes[0]
        self._current_swing_mode = None
        self._last_on_operation = None

        self._current_temperature = None
        self._current_humidity = None

        self._unit = hass.config.units.temperature_unit

        #Supported features
        self._support_flags = SUPPORT_FLAGS
        self._support_swing = False

        if self._humidity_sensor:
            self._support_flags = self._support_flags | SUPPORT_TARGET_HUMIDITY

        if self._swing_modes:
            self._support_flags = self._support_flags | SUPPORT_SWING_MODE
            self._current_swing_mode = self._swing_modes[0]
            self._support_swing = True

        self._temp_lock = asyncio.Lock()
        self._on_by_remote = False
        self._power_on_time = 0

        #Init the IR/RF controller
        self._controller = get_controller(controller or self._supported_controller)

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()

        if last_state is not None:
            self._hvac_mode = last_state.state
            self._current_fan_mode = last_state.attributes['fan_mode']
            self._current_swing_mode = last_state.attributes.get('swing_mode')
            self._target_temperature = last_state.attributes['temperature']
            self._target_humidity = last_state.attributes['humidity']

            if 'last_on_operation' in last_state.attributes:
                self._last_on_operation = last_state.attributes['last_on_operation']

            self._target_temperature_climate = self._target_temperature
            if 'temperature_climate' in last_state.attributes:
                self._target_temperature_climate = last_state.attributes['temperature_climate']

        if self._temperature_sensor:
            async_track_state_change(self.hass, self._temperature_sensor,
                                     self._async_temp_sensor_changed)

            temp_sensor_state = self.hass.states.get(self._temperature_sensor)
            if temp_sensor_state and temp_sensor_state.state != STATE_UNKNOWN:
                await self._async_update_temp(temp_sensor_state)

        if self._humidity_sensor:
            async_track_state_change(self.hass, self._humidity_sensor,
                                     self._async_humidity_sensor_changed)

            humidity_sensor_state = self.hass.states.get(self._humidity_sensor)
            if humidity_sensor_state and humidity_sensor_state.state != STATE_UNKNOWN:
                await self._async_update_humidity(humidity_sensor_state)

        if self._power_sensor:
            async_track_state_change(self.hass, self._power_sensor,
                                     self._async_power_sensor_changed)

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the climate device."""
        return self._name

    @property
    def state(self):
        """Return the current state."""
        if self.hvac_mode != HVAC_MODE_OFF:
            return self.hvac_mode
        return HVAC_MODE_OFF

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def min_temp(self):
        """Return the polling state."""
        return self._min_temperature

    @property
    def max_temp(self):
        """Return the polling state."""
        return self._max_temperature

    @property
    def min_humidity(self):
        """Return the polling state."""
        return self._min_humidity

    @property
    def max_humidity(self):
        """Return the polling state."""
        return self._max_humidity

    @property
    def target_humidity(self):
        """Return the temperature we try to reach."""
        return self._target_humidity

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        return self._precision

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        return self._operation_modes

    @property
    def hvac_mode(self):
        """Return hvac mode ie. heat, cool."""
        return self._hvac_mode

    @property
    def last_on_operation(self):
        """Return the last non-idle operation ie. heat, cool."""
        return self._last_on_operation

    @property
    def fan_modes(self):
        """Return the list of available fan modes."""
        return self._fan_modes

    @property
    def fan_mode(self):
        """Return the fan setting."""
        return self._current_fan_mode

    @property
    def swing_modes(self):
        """Return the swing modes currently supported for this device."""
        return self._swing_modes

    @property
    def swing_mode(self):
        """Return the current swing mode."""
        return self._current_swing_mode

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temperature

    @property
    def current_humidity(self):
        """Return the current humidity."""
        return self._current_humidity

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    @property
    def extra_state_attributes(self):
        """Platform specific attributes."""
        return {
            'temperature_climate': self._target_temperature_climate,
            'last_on_operation': self._last_on_operation,
            'device_code': self._device_code,
            'manufacturer': self._manufacturer,
            'supported_models': self._supported_models,
            'supported_controller': self._supported_controller,
            'commands_encoding': self._commands_encoding
        }

    def power_meter(self):
        if self._power_meter_sensor:
            powerMeter = self._power_meter_sensor and self.hass.states.get(self._power_meter_sensor).state
            _LOGGER.debug("power_meter=%s", powerMeter)
        return powerMeter or 0

    def power_sensor_is_switch(self):
        result = False
        if self._power_sensor:
            state = self.hass.states.get(self._power_sensor)
            device_class = state.attributes.get(ATTR_DEVICE_CLASS)
            result = device_class in switch.DEVICE_CLASSES
        return result

    # <internal> switch power_sensor on(True) or off(False). defaults to `on`
    async def async_power_sensor_switch_on(self, on = True):
        if on == switch.is_on(self.hass, self._power_sensor):
            return

        if on:
            switchOn = SERVICE_TURN_ON
            delayTime = self._delay_on
        else:
            switchOn = SERVICE_TURN_OFF
            delayTime = self._delay_off

        @callback
        async def _switch_cb():
            data = {ATTR_ENTITY_ID: self._power_sensor}
            await self.hass.services.async_call(
                HA_DOMAIN, switchOn, data, context=self._context
            )
            isSwitchOn = switch.is_on(self.hass, self._power_sensor)
            if on and isSwitchOn:
                self._power_on_time = time.time()
            elif not isSwitchOn:
                self._power_on_time = 0

        _LOGGER.debug("async_power_sensor_switch_on=%s delay=%s", switchOn, delayTime)
        async_call_later(self.hass, delayTime, _switch_cb)

    async def async_check_temperature(self, **kwargs):
        oldMode = self._hvac_mode
        if oldMode is HVAC_MODE_OFF:
            return
        diff_temp = self._target_temperature - self._current_temperature
        temperature = None
        isFullPower = self._max_power_meter and math.close(self.power_meter(), self._max_power_meter, rel_tol=50)
        minRunTime = self._run_time.total_seconds()
        # isCoolMode = self._hvac_mode in [HVAC_MODE_COOL, HVAC_MODE_DRY]
        if -diff_temp >= self._cold_tolerance:
            # current temperature > target temperature
            temperature = self._target_temperature
            self._hvac_mode = HVAC_MODE_COOL
            # need to cooling
            if math.isclose(temperature, self._target_temperature_climate, rel_tol=0.1):
                if self._power_on_time >= minRunTime and not isFullPower:
                    temperature = self._target_temperature_climate - 1
                elif oldMode == HVAC_MODE_COOL:
                    return
        elif diff_temp >= self._hot_tolerance or abs(diff_temp) < self._cold_tolerance:
            # current temperature < target temperature
            self._hvac_mode = HVAC_MODE_FAN_ONLY
            temperature = self._target_temperature
            if math.isclose(temperature, self._target_temperature_climate, rel_tol=0.1):
                if self._power_on_time >= minRunTime:
                    temperature = self._target_temperature_climate + 1
                elif oldMode == HVAC_MODE_FAN_ONLY:
                    return

        if temperature < self._min_temperature or temperature > self._max_temperature:
            _LOGGER.warning('The temperature value is out of min/max range')
            return

        if self._precision == PRECISION_WHOLE:
            temperature = round(temperature)
        else:
            temperature = round(temperature, 1)
        self._target_temperature_climate = temperature
        _LOGGER.debug("async_check_temperature: adjust target_temperature_climate=%s", temperature)

        await self.async_update_temperature(**kwargs)

    async def async_update_temperature(self, **kwargs):
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode:
            await self.async_set_hvac_mode(hvac_mode)
            return

        if not self._hvac_mode.lower() == HVAC_MODE_OFF:
            await self.send_command()

        await self.async_update_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperatures."""
        temperature = kwargs.get(ATTR_TEMPERATURE)

        if temperature is None:
            return

        if temperature < self._min_temperature or temperature > self._max_temperature:
            _LOGGER.warning('The temperature value is out of min/max range')
            return

        if self._precision == PRECISION_WHOLE:
            self._target_temperature = round(temperature)
        else:
            self._target_temperature = round(temperature, 1)

        if self._use_temperature_sensor:
            await self.async_check_temperature(**kwargs)
            return
        self._target_temperature_climate = self._target_temperature

        await self.async_update_temperature(**kwargs)

    async def async_set_humidity(self, **kwargs) -> None:
        """Set new target humidity."""
        humidity = kwargs.get(ATTR_HUMIDITY)

        if humidity is None:
            return
        if humidity < self._min_humidity or humidity > self._max_humidity:
            _LOGGER.warning('The humidity value is out of min/max range')
            return

        if self._precision == PRECISION_WHOLE:
            self._target_humidity = round(humidity)
        else:
            self._target_humidity = round(humidity, 1)

    async def async_set_hvac_mode(self, hvac_mode):
        """Set operation mode."""
        self._hvac_mode = hvac_mode
        isPowerSwitch = self.power_sensor_is_switch()

        if not hvac_mode == HVAC_MODE_OFF:
            self._last_on_operation = hvac_mode

        if hvac_mode != HVAC_MODE_OFF and isPowerSwitch:
            await self.async_power_sensor_switch_on(True)

        await self.send_command()
        await self.async_update_ha_state()

        if hvac_mode == HVAC_MODE_OFF and isPowerSwitch:
            await self.async_power_sensor_switch_on(False)

    async def async_set_fan_mode(self, fan_mode):
        """Set fan mode."""
        self._current_fan_mode = fan_mode

        if not self._hvac_mode.lower() == HVAC_MODE_OFF:
            await self.send_command()
        await self.async_update_ha_state()

    async def async_set_swing_mode(self, swing_mode):
        """Set swing mode."""
        self._current_swing_mode = swing_mode

        if not self._hvac_mode.lower() == HVAC_MODE_OFF:
            await self.send_command()
        await self.async_update_ha_state()

    async def async_turn_off(self):
        """Turn off."""
        await self.async_set_hvac_mode(HVAC_MODE_OFF)

    async def async_turn_on(self):
        """Turn on."""
        if self._last_on_operation is not None:
            await self.async_set_hvac_mode(self._last_on_operation)
        else:
            await self.async_set_hvac_mode(self._operation_modes[1])

    async def send_command(self):
        async with self._temp_lock:
            try:
                self._on_by_remote = False
                operation_mode = self._hvac_mode
                fan_mode = self._current_fan_mode
                swing_mode = self._current_swing_mode
                target_temperature = '{0:g}'.format(self._target_temperature_climate)
                _LOGGER.debug("operation_mode=%s, fan_mode=%s, swing_mode=%s, target_temperature=%s", operation_mode, fan_mode, swing_mode, target_temperature)

                if operation_mode.lower() == HVAC_MODE_OFF:
                    await self._controller.send(self._commands['off'], self)
                    return

                if 'on' in self._commands:
                    await self._controller.send(self._commands['on'], self)
                    await asyncio.sleep(self._delay)

                if self._support_swing == True:
                    await self._controller.send(
                        self._commands[operation_mode][fan_mode][swing_mode][target_temperature], self)
                else:
                    await self._controller.send(
                        self._commands[operation_mode][fan_mode][target_temperature], self)

            except Exception as e:
                _LOGGER.exception(e)

    async def _async_temp_sensor_changed(self, entity_id, old_state, new_state):
        """Handle temperature sensor changes."""
        if new_state is None:
            return

        await self._async_update_temp(new_state)
        await self.async_update_ha_state()

    async def _async_humidity_sensor_changed(self, entity_id, old_state, new_state):
        """Handle humidity sensor changes."""
        if new_state is None:
            return

        await self._async_update_humidity(new_state)
        await self.async_update_ha_state()

    async def _async_power_sensor_changed(self, entity_id, old_state, new_state):
        """Handle power sensor changes."""
        if new_state is None:
            return

        if old_state is not None and new_state.state == old_state.state:
            return

        if new_state.state == STATE_ON:
            if self._hvac_mode == HVAC_MODE_OFF:
                self._on_by_remote = True
            if self._power_sensor_restore_state == True and self._last_on_operation is not None:
                self._hvac_mode = self._last_on_operation
            else:
                self._hvac_mode = STATE_ON
            if self.power_sensor_is_switch():
                time.sleep(self._delay_on)
                await self.async_set_hvac_mode(self._hvac_mode)
            else:
                await self.async_update_ha_state()
        elif new_state.state == STATE_OFF:
            self._on_by_remote = False
            if self._hvac_mode != HVAC_MODE_OFF:
                self._hvac_mode = HVAC_MODE_OFF
            await self.async_update_ha_state()

    @callback
    async def _async_update_temp(self, state):
        """Update thermostat with latest state from temperature sensor."""
        try:
            if state.state != STATE_UNKNOWN and state.state != STATE_UNAVAILABLE:
                self._current_temperature = float(state.state)
                if self._use_temperature_sensor:
                  await self.async_check_temperature()
        except ValueError as ex:
            _LOGGER.error("Unable to update from temperature sensor: %s", ex)

    @callback
    async def _async_update_humidity(self, state):
        """Update thermostat with latest state from humidity sensor."""
        try:
            if state.state != STATE_UNKNOWN and state.state != STATE_UNAVAILABLE:
                self._current_humidity = float(state.state)
        except ValueError as ex:
            _LOGGER.error("Unable to update from humidity sensor: %s", ex)
