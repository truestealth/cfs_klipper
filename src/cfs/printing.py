"""Print lifecycle and retained virtual-SD metadata integration."""

import logging
import os


class Printing:
    def __init__(self, box):
        self.box = box

    def cmd_box_start_print(self, gcmd):
        box = self.box
        guard = box.host_guard()
        for address in range(1, 5):
            if box.box_state.get_Tn_data(address, "state") == "connect" and box.preloading_en:
                box.communication_set_pre_loading(address, "CLOSE", force=True)
                guard()
                box.communication_tighten_up_enable(address, "DISABLE")
                guard()

    def cmd_box_end_print(self, gcmd):
        box = self.box
        guard = box.host_guard()
        for address in range(1, 5):
            if box.box_state.get_Tn_data(address, "state") == "connect":
                if box.preloading_en:
                    box.communication_set_pre_loading(address, "OPEN", force=True)
                    guard()
                box.communication_tighten_up_enable(address, "ENABLE")
                guard()
        for tool in box.box_state.Tnn_map:
            box.box_state.Tnn_map[tool] = tool
        if not box.store.cleanup_recovery():
            raise OSError("Unable to clear CFS recovery metadata")
        guard()
        box.box_state.modify_Tn_data(0, 1, uppart="enable")
        box.set_head_runout_enabled(0)
        guard()

    def cmd_box_start_print_extrude_material(self, gcmd):
        box = self.box
        guard = box.lifecycle._report_guard(operation=False)
        guard()
        virtual_sd = box._printer.lookup_object("virtual_sdcard")
        guard()
        first = virtual_sd.get_first_Tn(None)
        guard()
        if first:
            box.start_print_retraction_distance = gcmd.get_int("START_PRINT", 0)
            box.gcode.run_script_from_command(str(first))
            guard()

    def record_tn_cur_tnn(self, tnn):
        box = self.box
        virtual_sd = box._printer.lookup_object("virtual_sdcard")
        gcode_move = box._printer.lookup_object("gcode_move")
        try:
            name = virtual_sd.current_file.name if virtual_sd.current_file else ""
            gcode_move.recordPrintFileName(virtual_sd.print_file_name_path, name, tn_cur_tnn=tnn)
        except Exception:
            logging.exception("Could not record CFS tool metadata")

    def has_flushing_sign(self):
        if os.path.exists(self.box.flushing_sign):
            os.remove(self.box.flushing_sign)
            return True
        return False
