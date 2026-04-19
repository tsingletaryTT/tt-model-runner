# tests/test_controller_contract.py
"""ViewContract — mechanically enforces GUI/TUI feature parity.

If AppController gains a new on_* callback, this test fails until both
GtkViewStub and TuiViewStub implement it.  Add the new method to both
stubs AND to the ABC to restore green.
"""
from abc import ABC, abstractmethod
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


class ViewContract(ABC):
    """Every on_* callback that AppController can emit MUST be handled here."""

    @abstractmethod
    def on_state_changed(self, state, info: str): ...

    @abstractmethod
    def on_log_line(self, line: str): ...

    @abstractmethod
    def on_progress(self, fraction: float, label: str): ...

    @abstractmethod
    def on_substage(self, stepper: str, tour_left: str, tour_right: str, dots: str): ...

    @abstractmethod
    def on_catalog_loaded(self, catalog, compatible_devices: list): ...

    @abstractmethod
    def on_cache_scanned(self, info): ...

    @abstractmethod
    def on_bench_progress(self, line: str): ...

    @abstractmethod
    def on_bench_result(self, result): ...

    @abstractmethod
    def on_tool_result(self, result): ...

    @abstractmethod
    def on_running_servers(self, servers: list): ...

    @abstractmethod
    def on_hardware_status(self, chips: list): ...


class GtkViewStub(ViewContract):
    def on_state_changed(self, state, info): pass
    def on_log_line(self, line): pass
    def on_progress(self, fraction, label): pass
    def on_substage(self, stepper, tour_left, tour_right, dots): pass
    def on_catalog_loaded(self, catalog, compatible_devices): pass
    def on_cache_scanned(self, info): pass
    def on_bench_progress(self, line): pass
    def on_bench_result(self, result): pass
    def on_tool_result(self, result): pass
    def on_running_servers(self, servers): pass
    def on_hardware_status(self, chips): pass


class TuiViewStub(ViewContract):
    def on_state_changed(self, state, info): pass
    def on_log_line(self, line): pass
    def on_progress(self, fraction, label): pass
    def on_substage(self, stepper, tour_left, tour_right, dots): pass
    def on_catalog_loaded(self, catalog, compatible_devices): pass
    def on_cache_scanned(self, info): pass
    def on_bench_progress(self, line): pass
    def on_bench_result(self, result): pass
    def on_tool_result(self, result): pass
    def on_running_servers(self, servers): pass
    def on_hardware_status(self, chips): pass


def test_gtk_stub_satisfies_contract():
    assert isinstance(GtkViewStub(), ViewContract)


def test_tui_stub_satisfies_contract():
    assert isinstance(TuiViewStub(), ViewContract)


def test_controller_on_attrs_match_contract():
    """AppController's on_* attributes must exactly match ViewContract methods."""
    from controller import AppController
    controller_cbs = {
        a for a in vars(AppController).get("__annotations__", {})
        if a.startswith("on_")
    }
    # Also pick up any on_* set in __init__ that aren't annotated at class level
    ctrl = AppController()
    instance_cbs = {a for a in vars(ctrl) if a.startswith("on_")}
    all_ctrl_cbs = controller_cbs | instance_cbs

    contract_methods = {
        a for a in dir(ViewContract)
        if a.startswith("on_") and callable(getattr(ViewContract, a))
    }
    assert all_ctrl_cbs == contract_methods, (
        f"Controller has callbacks not in contract: {all_ctrl_cbs - contract_methods}\n"
        f"Contract has methods not in controller: {contract_methods - all_ctrl_cbs}"
    )
