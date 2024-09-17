"""Nord pool cheapet hours binary sensor."""

from datetime import date, datetime, timedelta
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from .coordinator import EnergyManagementCoordinator
from .exceptions import InvalidEntityState, InvalidInput, ValueNotFound
from .helpers import (
    convert_datetime,
    from_str_to_time,
    merge_two_dicts,
    time_in_between,
)
from .math import (
    calculate_non_sequential_cheapest_hours,
    calculate_sequential_cheapest_hours,
)

_LOGGER = logging.getLogger(__name__)


class NordPoolCheapestHoursBinarySensor(BinarySensorEntity):
    """Cheapest hours sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        nordpool_entity,
        unique_id,
        name,
        first_hour,
        last_hour,
        starting_today,
        number_of_hours,
        sequential,
        coordinator: EnergyManagementCoordinator,
        failsafe_starting_hour=None,
        inversed=False,
    ) -> None:
        """Init sensor."""
        self._nordpool_entity = nordpool_entity
        self._attr_unique_id = unique_id.replace(" ", "_")
        self._attr_name = name
        self._attr_icon = "mdi:clock"
        self._attr_is_on = STATE_UNKNOWN
        self._coordinator = coordinator
        self._first_hour = first_hour
        self._last_hour = last_hour
        self._starting_today = starting_today
        self._sequential = sequential
        self._failsafe_starting_hour = failsafe_starting_hour
        self._number_of_hours = number_of_hours
        self._inversed = inversed

        self.hass = hass
        # Data
        self._data = self._coordinator.get_data("binary_sensor." + self._attr_unique_id)
        if self._data is None:
            self._data = {}
        self._nordpool = None

    async def async_update(self) -> None:
        """Update sensor."""
        await self._async_operate()

    @property
    def extra_state_attributes(self) -> dict:
        """Return all the data."""
        return merge_two_dicts(self._data, self._construct_attributes())

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        if self._data is None:
            return False

        items = self._data.get("list")
        if items is None or len(items) == 0:
            # No valid data, check failsafe
            _LOGGER.debug("No valid data found. Check failsafe")
            return self._is_failsafe()

        # We got valid data, check the actual list of items
        values = convert_datetime(items)

        if values is None:
            return False

        for value in values:
            start: datetime = dt_util.as_local(value.get("start"))
            end: datetime = dt_util.as_local(value.get("end"))

            if dt_util.now() >= start:
                if dt_util.now() <= end:
                    return True

        return False

    async def _async_operate(self) -> None:
        if self._data is None:
            self._data = self._coordinator.get_data(self._attr_unique_id)

        # Don't update values if we are already on failsafe as we don't want to interrupt it
        if self._is_failsafe():
            return None

        # Check if our data is valid and we do not need to do anything
        if self._is_fetched_today():  # Data valid for today
            _LOGGER.debug(
                "Local entity data is still valid for %s", self._attr_unique_id
            )
            if self._is_expired() is False:
                return None

        # No valid data found from store either, try get new
        _LOGGER.debug(
            "No today fetch done for %s or value is expired. Request new data from nord pool integration",
            self._attr_unique_id,
        )

        # Update number of hours.. this actually needs to be stored in the data..
        try:
            self._update_number_of_hours()
        except InvalidEntityState:
            return None

        self._swap_list_if_needed()
        self._data["failsafe"] = self._create_failsafe()

        try:
            self._update_from_nordpool()
        except ValueNotFound:
            _LOGGER.debug("Could not get the latest data from nordpool integration")
            if self._is_expired():
                self._data.pop("list", None)
            return None

        cheapest = None

        # Use proper method if sequential or non-sequential
        try:
            if self._sequential:
                cheapest = calculate_sequential_cheapest_hours(
                    self._nordpool.attributes["today"],
                    self._nordpool.attributes["tomorrow"],
                    self._data["active_number_of_hours"],
                    self._starting_today,
                    self._first_hour,
                    self._last_hour,
                    self._inversed,
                )
            else:
                cheapest = calculate_non_sequential_cheapest_hours(
                    self._nordpool.attributes["today"],
                    self._nordpool.attributes["tomorrow"],
                    self._data["active_number_of_hours"],
                    self._starting_today,
                    self._first_hour,
                    self._last_hour,
                    self._inversed,
                )
        except InvalidInput:
            return None

        # Construct new data from calculated hours
        if self._is_expired():
            self._set_list(cheapest)
            self._data["updated_at"] = dt_util.now()
            self._data["expiration"] = self._create_expiration()
        elif (
            self._data["list"] != cheapest
        ):  # Not expired, but data is not the same. Set to list_next
            # Data is not the same, set the next
            self._data["list_next"] = cheapest

        self._data["fetch_date"] = self._create_fetch_date()

        await self._coordinator.async_set_data(
            self._attr_unique_id,
            self._attr_name,
            self.__class__.__name__,
            self._data,
        )

    def _swap_list_if_needed(self) -> bool:
        """Swap the list_next to list if needed. Returns true if list was swapped."""
        if self._is_expired():  # Data is expired
            if list_next := self._data.get("list_next"):
                self._set_list(list_next)
                self._data.pop("list_next", None)
                self._data["expiration"] = self._create_expiration()
                return True
            self._data.pop("list", None)

        return False

    def _set_list(self, list: dict) -> None:
        """Set list data."""
        self._data["list"] = list
        self._data["updated_at"] = dt_util.now()

    def _is_expired(self) -> bool:
        """Check if data is expired."""
        if self._data is not None:
            if self._data.get("list") is None or {}:
                return True

            if self._data.get("expiration") is None:
                return True

            expires = dt_util.as_local(self._data.get("expiration"))
            if expires is not None:
                if expires > dt_util.now():
                    return False
        return True

    def _is_fetched_today(self) -> bool:
        if self._data is None:
            return False

        fetch_date = self._data.get("fetch_date")
        if fetch_date is not None and fetch_date == dt_util.start_of_local_day().date():
            return True
        return False

    def _create_failsafe(self) -> dict | None:
        """Construct failsafe dictionary."""
        if self._failsafe_starting_hour is None:
            return None

        start = dt_util.now().replace(
            hour=self._failsafe_starting_hour, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(hours=self._data["active_number_of_hours"])
        return {"start": start.time(), "end": end.time()}

    def _create_expiration(self) -> datetime:
        """Calculate value expiration."""
        if items := self._data.get("list"):
            values = convert_datetime(items)
            if len(values) > 0:
                if last_start := values[-1].get("start"):
                    return (last_start.replace(hour=self._last_hour)) + timedelta(
                        hours=1
                    )
            return None

        # No list, data should expire immediately
        return dt_util.start_of_local_day()

    def _create_fetch_date(self) -> date:
        """Return fetch date."""
        return dt_util.start_of_local_day().date()

    def _update_from_nordpool(self) -> None:
        np = self.hass.states.get(self._nordpool_entity)

        if np is None:
            _LOGGER.debug(
                "Got empty data from Norpool entity %s ", self._nordpool_entity
            )
            raise ValueNotFound
        if np.attributes.get("today") is None:
            _LOGGER.debug(
                "No values for today in Norpool entity %s ", self._nordpool_entity
            )
            raise ValueNotFound
        if np.attributes.get("tomorrow_valid") is False or None:
            _LOGGER.debug(
                "No values for tomorrow_valid in Norpool entity %s ",
                self._nordpool_entity,
            )
            raise ValueNotFound
        if np.attributes.get("tomorrow") is None:
            _LOGGER.warning(
                "No values for tomorrow in Norpool entity %s ", self._nordpool_entity
            )
            raise ValueNotFound

        self._nordpool = np

    def _is_failsafe(self) -> bool:
        if (
            self._data is None
        ):  # TODO: If data is none, we most probably need to check the failsafe value instead of just returning false!
            return False

        if self._is_expired() is False:
            return False  # Data not expired yet.

        if failsafe := self._data.get("failsafe"):
            start = from_str_to_time(failsafe.get("start"))
            end = from_str_to_time(failsafe.get("end"))

            return time_in_between(dt_util.now().time(), start, end)

        return False

    def _construct_attributes(self) -> dict:
        return {
            "first_hour": self._first_hour,
            "last_hour": self._last_hour,
            "starting_today": self._starting_today,
            "number_of_hours": self._number_of_hours,
            "failsafe_starting_hour": self._failsafe_starting_hour,
            "is_sequential": self._sequential,
            "failsafe_active": self._is_failsafe(),
            "inversed": self._inversed,
        }

    def _update_number_of_hours(self) -> None:
        if isinstance(self._number_of_hours, int):
            self._data["active_number_of_hours"] = self._number_of_hours
            return None

        value = self.hass.states.get(self._number_of_hours).state
        if value is not None:
            self._data["active_number_of_hours"] = int(float(value))
        else:
            _LOGGER.error(
                "Could not get entity state for cheapest hours number of hours!"
            )
            raise InvalidEntityState
