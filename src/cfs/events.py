"""Process slot identity events without treating them as mechanical completion."""

import logging

from .lifecycle import LifecycleStopped
from .native import build_request


class SlotEvents:
    def __init__(self, box):
        self.box = box

    def _restore(self, address, slot, index):
        state = self.box.box_state

        def saved(field):
            return state.tn_save_data["T%d" % address][field][index]

        def put(field, value):
            state.modify_Tn_data(address, value, field, slot)

        vendor = saved("vender")
        if vendor == "-1":
            for field in ("vender", "color_value", "material_type"):
                put(field, "unknown")
        elif vendor == "unknown":
            put("vender", vendor)
            color = saved("color_value")
            if color not in ("unknown", "-1"):
                put("color_value", color)
                put("material_type", saved("material_type"))
        else:
            for field in ("vender", "remain_len", "color_value", "material_type"):
                put(field, saved(field))

    def schedule_rfid(self, address, mask):
        box = self.box
        # Metadata work may run after a T operation completes, but never on a
        # replacement device that subsequently acquired the same address.
        guard = box.lifecycle._report_guard(address, operation=False)
        guard()

        def refresh(eventtime):
            try:
                guard()
            except LifecycleStopped:
                return
            for command in ("GET_RFID", "GET_REMAIN_LEN"):
                box.send_native(build_request(address, command, bytes((mask,))))
                guard()

        box.lifecycle.schedule(refresh)

    def process(self, address, data):
        box, state = self.box, self.box.box_state
        guard = box.host_guard(address)
        if len(data) != 4:
            raise ValueError("a slot event must contain four values")
        for index, event in enumerate(data):
            guard()
            slot = "ABCD"[index]
            if event == 2:
                state.modify_Tn_data(address, "none", "vender", slot)
            elif event in (1, 3):
                if box.print_stats.state in ("printing", "paused"):
                    self._restore(address, slot, index)
                elif box.preloading_en and event == 3:
                    try:
                        self.schedule_rfid(address, 1 << index)
                    except LifecycleStopped:
                        raise
                    except Exception:
                        logging.exception("Could not schedule CFS slot metadata refresh")
                else:
                    state.modify_Tn_data(address, "unknown", "vender", slot)
                    if not box.preloading_en:
                        # The vendor setter above also updates saved metadata.
                        # Read it afterwards, preserving that observable order.
                        saved = state.tn_save_data["T%d" % address]
                        color = saved["color_value"][index]
                        if saved["vender"][index] == "unknown" and color not in ("unknown", "-1"):
                            state.modify_Tn_data(address, color, "color_value", slot)
                            state.modify_Tn_data(address, saved["material_type"][index], "material_type", slot)
                        else:
                            state.modify_Tn_data(address, "unknown", "color_value", slot)
                            state.modify_Tn_data(address, "unknown", "material_type", slot)
            guard()
        state.update_same_material_list(box.store.get_material_name)
        guard()
