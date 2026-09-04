from celery import shared_task, group
from megacellcnc.models import Device, Slot, Chemistry, CellTestData
from celery.exceptions import SoftTimeLimitExceeded
from mccprolib.api import MegacellCharger
from megacellcnc.functions import portscan, add_new_cell
import base64
import logging
from django.db import transaction
from django.core import serializers
from itertools import groupby
from operator import itemgetter
from django.db.models import Avg, Max
from django.utils import timezone

logger = logging.getLogger(__name__)

ACTIVE_TEST_STATES = frozenset([
    "LVC Charging", "Started Charging", "Cooldown", "Started Discharging", "ESR Reading", "Resting",
    "Started Store Charging", "Started Store Discharging", "Dispose started", "mCap Started Charging",
    "mCap Started Discharging", "mCap Store Charging", "mCap Store Discharging", "Wait For ESR Test",
    "Cell rest 5 Min",
])

COMPLETE_STATES = frozenset(["Stored"])


def constrain_value(min_allowed, max_allowed, actual_value):
    return max(min_allowed, min(max_allowed, actual_value))


def _cell_num(cell, key, default=0):
    value = cell.get(key, default)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _slot_count_from_identity(device_type):
    if not isinstance(device_type, dict):
        return None
    cht = device_type.get("ChT")
    if cht in ("MCCPro", "MCCReg"):
        return device_type.get("CeC") or device_type.get("ByC")
    if cht == "MCC" or "McC" in device_type:
        return device_type.get("ByC") or device_type.get("CeC")
    return device_type.get("CeC") or device_type.get("ByC")


def _is_test_finished(slot):
    if slot.state in COMPLETE_STATES:
        return True
    current = abs(slot.current or 0)
    capacity = slot.capacity or 0
    if (
        current < 50
        and capacity > 0
        and slot.state not in ACTIVE_TEST_STATES
        and slot.state != "Not Inserted"
    ):
        return True
    return False


def update_cell_data(device, slot):
    current = slot.current or 0
    if (current > 50 or current < -50) and not slot.saved:
        add_new_cell(device, slot)

    cell = slot.active_cell

    if cell:
        cell.voltage = slot.voltage
        cell.capacity = slot.capacity
        cell.esr = slot.esr
        if slot.state == "LVC Charging" or slot.state == "Started Charging":
            cell.charge_duration = slot.action_running_time

        if slot.state == "Started Discharging":
            if cell.charge_duration > 0 and slot.action_running_time > cell.charge_duration:
                cell.discharge_duration = slot.action_running_time - cell.charge_duration
            else:
                cell.discharge_duration = slot.action_running_time

        cell.cycles_count = slot.completed_cycles
        cell.min_voltage = slot.min_volt
        cell.max_voltage = slot.max_volt
        cell.store_voltage = slot.store_volt
        cell.testing_current = device.discharge_current
        cell.status = slot.state
        cell.test_duration = slot.action_running_time
        cell.save()

        if slot.state in ACTIVE_TEST_STATES:
            new_data = CellTestData(
                cell=cell,
                voltage=slot.voltage,
                current=slot.current,
                capacity=slot.capacity,
                charging_capacity=slot.charge_capacity,
                status=slot.state,
                temperature=slot.temperature,
                cycle_number=slot.completed_cycles,
                timestamp=timezone.now()
            )
            new_data.save()

        if cell.available != "Yes" and _is_test_finished(slot):
            mark_cell_available(cell, removed=False)


def apply_test_stats(cell):
    average_charge_temp_data = cell.test_data.filter(status='Started Charging').aggregate(Avg('temperature'))
    average_charge_temperature = average_charge_temp_data.get('temperature__avg') or 0

    average_discharge_temp_data = cell.test_data.filter(status='Started Discharging').aggregate(Avg('temperature'))
    average_discharge_temperature = average_discharge_temp_data.get('temperature__avg') or 0

    max_temp_charging_data = cell.test_data.filter(status='Started Charging').aggregate(Max('temperature'))
    max_temp_charging = max_temp_charging_data.get('temperature__max') or 0

    max_temp_discharging_data = cell.test_data.filter(status='Started Discharging').aggregate(Max('temperature'))
    max_temp_discharging = max_temp_discharging_data.get('temperature__max') or 0

    cell.avg_temp_charging = average_charge_temperature
    cell.avg_temp_discharging = average_discharge_temperature
    cell.max_temp_charging = max_temp_charging
    cell.max_temp_discharging = max_temp_discharging


def mark_cell_available(cell, *, removed=False):
    if not cell:
        return
    if not cell.removal_date:
        cell.removal_date = timezone.now()
    cell.available = "Yes"
    if removed:
        cell.status = "Removed"
    apply_test_stats(cell)
    cell.save()


def cell_test_complete(cell):
    mark_cell_available(cell, removed=True)


def update_slot_data(device_model, tester, device_slot_count):
    slots = device_model.slots.all()
    current_slot_count = slots.count()

    if current_slot_count != device_slot_count:
        with transaction.atomic():
            slots.delete()
            for slot_num in range(1, device_slot_count + 1):
                Slot.objects.create(device=device_model, slot_number=slot_num)
        slots = device_model.slots.all()

    data = tester.get_cells_data()
    cells_list = data.get("cells") if isinstance(data, dict) else None
    if not cells_list:
        logger.warning("update_slot_data: no cells from %s", device_model.ip)
        return

    use_gid = device_model.type == "MCCPro" and "GiD" in cells_list[0]
    group_key = "GiD" if use_gid else "CiD"
    try:
        data_sorted = sorted(cells_list, key=itemgetter(group_key))
        groups = groupby(data_sorted, key=itemgetter(group_key))
    except KeyError:
        logger.warning("update_slot_data: missing %s in cells from %s", group_key, device_model.ip)
        return

    for gid, items in groups:
        try:
            slot_num = gid + 1
            cell = next(items)

            slot = device_model.slots.get(slot_number=slot_num)
            slot.voltage = _cell_num(cell, "VlT")
            slot.current = int(_cell_num(cell, "AmP"))
            slot.capacity = _cell_num(cell, "CaP")
            slot.charge_capacity = _cell_num(cell, "CCa")
            slot.state = cell.get("StS") or ""

            if slot.saved and slot.state == "Not Inserted":
                slot.saved = False
                if slot.active_cell:
                    cell_test_complete(slot.active_cell)
                slot.active_cell = None

            slot.esr = _cell_num(cell, "esr")
            slot.action_running_time = _cell_num(cell, "AcL")
            slot.discharge_cycles_set = int(_cell_num(cell, "DiC"))
            slot.completed_cycles = int(_cell_num(cell, "CoC"))
            slot.temperature = _cell_num(cell, "TmP")
            slot.max_volt = _cell_num(cell, "MaV")
            slot.store_volt = _cell_num(cell, "StV")
            slot.min_volt = _cell_num(cell, "MiV")
            slot.save()

            update_cell_data(device_model, slot)
        except Slot.DoesNotExist:
            logger.warning("update_slot_data: slot %s missing on %s", slot_num, device_model.ip)
        except Exception:
            logger.exception("update_slot_data: failed slot on %s", device_model.ip)


@shared_task(soft_time_limit=10, time_limit=15)
def check_device_status(device_id):

    device = Device.objects.get(pk=device_id)

    res_dict = {}
    try:
        portscan(80, device.ip, res_dict)
        print(res_dict)
    except Exception:
        pass

    if device.ip not in res_dict:
        device.status = "offline"
        device.save()
        return False

    tester = MegacellCharger(device.ip)
    identity = tester.device_type
    if not identity or identity == "Unknown" or not isinstance(identity, dict):
        logger.warning("check_device_status: unknown identity for %s", device.ip)
        return False

    if "ChT" in identity:
        device.type = identity["ChT"]
    elif "McC" in identity:
        device.type = "MCC"
    else:
        logger.warning("check_device_status: no ChT/McC for %s identity=%s", device.ip, identity)
        return False

    slot_count = _slot_count_from_identity(identity)
    if not slot_count:
        logger.warning("check_device_status: no slot count for %s identity=%s", device.ip, identity)
        return False

    device.status = "online"
    device.save()
    update_slot_data(device, tester, slot_count)
    return True


@shared_task
def check_all_devices():
    try:
        devices = Device.objects.all()
        if devices.exists():
            task_group = group(check_device_status.s(device.id) for device in devices)
            result_group = task_group.apply_async()
            return result_group
        else:
            # No devices to check
            return None
    except Exception:
        # Database tables not migrated yet or error accessing devices
        return None


@shared_task
def dispatch_command(data, request_data, action_type):
    deviceId = data.get('deviceId')

    try:
        device = Device.objects.get(id=deviceId)
    # Now you can use 'device' object for further operations
    except Device.DoesNotExist:
        return "Fail"

    tester = MegacellCharger(device.ip)
    if action_type == "regular":
        result = tester.set_cells(request_data)
        return result

    elif action_type == "macro":
        result = tester.set_cells_macro(request_data)

        return result

    else:
        return False


@shared_task
def get_device_config(device_id):

    device = Device.objects.get(pk=device_id)

    res_dict = {}
    try:
        portscan(80, device.ip, res_dict)
    except Exception:
        pass

    if device.ip not in res_dict:
        return {}, {}, ""

    try:
        tester = MegacellCharger(device.ip)
    except Exception as e:
        logger.warning("get_device_config: connect failed for %s: %s", device.ip, e)
        return {}, {}, ""

    if tester.device_type and "ChT" in tester.device_type:

        if tester.device_type["ChT"] == "MCCPro":
            chems = Chemistry.objects.filter(device_type="MCCPro")
            mccpro_chemistries_json = serializers.serialize('json', chems)
            data = {"CiD": 0}
            device_conf = tester.get_cell_chemistry(data)
            # Celery JSON result backend cannot carry raw bytes cleanly — use base64.
            if isinstance(device_conf, (bytes, bytearray)):
                device_conf = base64.b64encode(bytes(device_conf)).decode("ascii")
            return device_conf, mccpro_chemistries_json, tester.device_type["FwV"]

        elif tester.device_type["ChT"] == "MCCReg":
            chems = Chemistry.objects.filter(device_type="MCC")
            mcc_chemistries_json = serializers.serialize('json', chems)
            data = {"CiD": 0}
            device_conf = tester.get_cell_chemistry(data)
            if isinstance(device_conf, (bytes, bytearray)):
                device_conf = base64.b64encode(bytes(device_conf)).decode("ascii")
            return device_conf, mcc_chemistries_json, tester.device_type["FwV"]

        # Classic MCC sometimes reports ChT == "MCC" (not Pro/Reg) — use JSON config path.
        elif tester.device_type.get("ChT") == "MCC":
            chems = Chemistry.objects.filter(device_type="MCC")
            mcc_chemistries_json = serializers.serialize("json", chems)
            device_conf = tester.get_config()
            fw = tester.device_type.get("FwV") or tester.device_type.get("McC", "")
            return device_conf, mcc_chemistries_json, fw

    elif tester.device_type and 'McC' in tester.device_type:
        chems = Chemistry.objects.filter(device_type="MCC")
        mcc_chemistries_json = serializers.serialize('json', chems)
        device_conf = tester.get_config()
        return device_conf, mcc_chemistries_json, tester.device_type["McC"]

    return {}, {}, ""


def _mccpro_chemistry_payload(data, chem_id, slot_index):
    maxVolt = constrain_value(3.5, 4.24, float(data["maxVoltage"]))
    minVolt = constrain_value(1.0, 4.0, float(data["minVoltage"]))
    sVolt = constrain_value(2.5, 4.24, float(data["storeVoltage"]))
    maxCap = constrain_value(100, 999999, int(data["maxCapacity"]))
    chgCur = constrain_value(500, 4500, int(data["chargingCurrent"]))
    pChgCur = constrain_value(128, 2048, int(data["prechargeCurrent"]))
    terChgCur = constrain_value(128, 2048, int(data["termChargingCurrent"]))
    dchgRes = constrain_value(1, 10, float(data["dischargeResistance"]))
    dchgMod = constrain_value(0, 2, int(data["dischargeMode"]))
    maxTemp = constrain_value(0, 55, int(data["maxTemp"]))
    LmR = constrain_value(5, 999999, int(data["maxLowVoltTime"]))
    McH = constrain_value(5, 999999, int(data["chargingTimeout"]))
    DiC = constrain_value(1, 999999, int(data["dischargeCycles"]))
    chem_id = constrain_value(1, 16, int(chem_id))
    return {
        "Chem": {
            "id": chem_id,
            "name": "MegaCNC",
            "maxVolt": maxVolt * 1000,
            "minVolt": minVolt * 1000,
            "sVolt": sVolt * 1000,
            "maxCap": maxCap,
            "chgCur": chgCur,
            "pChgCur": pChgCur,
            "terChgCur": terChgCur,
            "dchgCur": constrain_value(100, 3000, int(data["dischargingCurrent"])),
            "dchgRes": dchgRes,
            "dchgMod": dchgMod,
            "maxTemp": maxTemp,
            "LmR": LmR,
            "McH": McH,
            "DiC": DiC,
        },
        "CiD": slot_index,
    }


@shared_task
def save_device_config(device_id, data):

    required = (
        "deviceName", "maxVoltage", "minVoltage", "storeVoltage", "chargingCurrent",
        "dischargingCurrent", "maxTemp", "dischargeCycles", "chargingTimeout",
    )
    for key in required:
        if key not in data or data[key] in (None, ""):
            logger.warning("save_device_config: missing field %s", key)
            return False

    try:
        device = Device.objects.get(pk=device_id)
    except Device.DoesNotExist:
        return False

    res_dict = {}
    try:
        portscan(80, device.ip, res_dict)
        print(res_dict)
    except Exception:
        pass

    if device.ip not in res_dict:
        return False

    try:
        tester = MegacellCharger(device.ip)
        if not tester.device_type:
            return False

        maxVolt = constrain_value(3.5, 4.24, float(data["maxVoltage"]))
        minVolt = constrain_value(1.0, 4.0, float(data["minVoltage"]))
        sVolt = constrain_value(2.5, 4.24, float(data["storeVoltage"]))
        maxCap = constrain_value(100, 999999, int(data["maxCapacity"]))
        chgCur = constrain_value(500, 4500, int(data["chargingCurrent"]))
        pChgCur = constrain_value(128, 2048, int(data["prechargeCurrent"]))
        terChgCur = constrain_value(128, 2048, int(data["termChargingCurrent"]))
        dchgRes = constrain_value(1, 10, float(data["dischargeResistance"]))
        dchgMod = constrain_value(0, 2, int(data["dischargeMode"]))
        maxTemp = constrain_value(0, 55, int(data["maxTemp"]))
        LmR = constrain_value(5, 999999, int(data["maxLowVoltTime"]))
        McH = constrain_value(5, 999999, int(data["chargingTimeout"]))
        DiC = constrain_value(1, 999999, int(data["dischargeCycles"]))

        cellsToGroup = constrain_value(1, 16, int(data["cellsToGroup"]))
        cellsPerGroup = constrain_value(1, 16, int(data["cellsPerGroup"]))
        tempSource = constrain_value(0, 1, int(data["tempSource"]))

        # Saving device data for display purposes
        device.name = data["deviceName"]
        device.discharge_mode = dchgMod
        device.discharge_current = int(data["dischargingCurrent"])
        device.cell_per_group = cellsPerGroup
        device.cell_to_group = cellsToGroup

        device.save()

        if tester.device_type and "ChT" in tester.device_type:
            mccpro_required = (
                "prechargeCurrent", "termChargingCurrent", "dischargeResistance", "dischargeMode",
                "maxLowVoltTime", "maxCapacity", "cellsToGroup", "cellsPerGroup", "applyToSlot", "tempSource",
            )
            for key in mccpro_required:
                if key not in data or data[key] in (None, ""):
                    logger.warning("save_device_config MCCPro: missing field %s", key)
                    return False

            chem_id = int(data.get("chemistry_id", 5))
            apply_all = bool(data.get("applyToAllSlots", False))

            if apply_all or tester.device_type["ChT"] == "MCCReg":
                for slot in range(16):
                    tester.set_cell_chemistry(_mccpro_chemistry_payload(data, chem_id, slot))
            else:
                apply_to_slot = constrain_value(1, 16, int(data["applyToSlot"]))
                tester.set_cell_chemistry(
                    _mccpro_chemistry_payload(data, chem_id, apply_to_slot - 1)
                )

            tester.set_hardware_config(tempSource, cellsToGroup, cellsPerGroup, 0)
            return True

        if tester.device_type and 'McC' in tester.device_type:
            config = {
                "ChC": True,
                "MaV": maxVolt,
                "StV": sVolt,
                "MiV": minVolt,
                "MaT": maxTemp,
                "DiC": DiC,
                "LmV": 0.3,
                "LcV": 3.6,
                "LmD": 1.1,
                "LmR": 500,
                "McH": int(data["chargingTimeout"]),
                "LcR": 200,
                "MsR": 2000,
                "DiR": constrain_value(100, 990, int(data["dischargingCurrent"])),
                "CcO": 1,
                "DcO": 1,
            }
            tester.set_config(config)
            return True

        return False
    except (KeyError, TypeError, ValueError) as e:
        logger.exception("save_device_config failed: %s", e)
        return False
