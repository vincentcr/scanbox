import contextlib
import io
import unittest
from unittest import mock

from scanbox import paths, proc, ui, vm


class CapabilityPlanTests(unittest.TestCase):
    def test_wsd_plan_contains_no_hp_capability(self) -> None:
        self.assertEqual(
            vm._capability_closure((vm.Capability.WSD,)),
            (vm.Capability.CORE, vm.Capability.WSD),
        )

    def test_legacy_plan_contains_every_hpaio_dependency(self) -> None:
        self.assertEqual(
            vm._capability_closure((vm.Capability.HP_PLUGIN,)),
            (
                vm.Capability.CORE,
                vm.Capability.HPLIP,
                vm.Capability.HP_PLUGIN,
            ),
        )

    @mock.patch("scanbox.vm.provision_capability")
    @mock.patch("scanbox.vm.is_capability_provisioned", return_value=False)
    @mock.patch("scanbox.vm.ensure_runtime")
    def test_wsd_ensure_starts_runtime_then_provisions_only_core_and_wsd(
        self, ensure_runtime, is_provisioned, provision
    ) -> None:
        vm.ensure_wsd()

        ensure_runtime.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in is_provisioned.call_args_list],
            [vm.Capability.CORE, vm.Capability.WSD],
        )
        self.assertEqual(
            [call.args[0] for call in provision.call_args_list],
            [vm.Capability.CORE, vm.Capability.WSD],
        )

    @mock.patch("scanbox.vm.sync_lib")
    @mock.patch("scanbox.vm.provision_capability")
    @mock.patch("scanbox.vm.is_capability_provisioned", return_value=True)
    @mock.patch("scanbox.vm.ensure_runtime")
    def test_existing_legacy_vm_is_reused_without_reprovisioning(
        self, ensure_runtime, is_provisioned, provision, sync_lib
    ) -> None:
        vm.ensure_hplip()

        ensure_runtime.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in is_provisioned.call_args_list],
            [
                vm.Capability.CORE,
                vm.Capability.HPLIP,
                vm.Capability.HP_PLUGIN,
            ],
        )
        provision.assert_not_called()
        sync_lib.assert_called_once_with()

    @mock.patch("scanbox.vm.sync_lib")
    @mock.patch("scanbox.vm.provision_capability")
    @mock.patch("scanbox.vm.is_capability_provisioned", return_value=False)
    @mock.patch("scanbox.vm.ensure_runtime")
    def test_legacy_ensure_provisions_full_hpaio_stack(
        self, ensure_runtime, is_provisioned, provision, sync_lib
    ) -> None:
        vm.ensure_hplip()

        self.assertEqual(
            [call.args[0] for call in provision.call_args_list],
            [
                vm.Capability.CORE,
                vm.Capability.HPLIP,
                vm.Capability.HP_PLUGIN,
            ],
        )
        sync_lib.assert_called_once_with()


class CapabilityProbeTests(unittest.TestCase):
    def test_each_capability_has_an_independent_guest_probe(self) -> None:
        commands = []

        def run(command, **_kwargs):
            commands.append(list(command))
            return proc.Result(0, "", "")

        with mock.patch("scanbox.vm.proc.run", side_effect=run):
            for capability in vm.Capability:
                self.assertTrue(vm.is_capability_provisioned(capability))

        probes = {capability: " ".join(command) for capability, command in zip(
            vm.Capability, commands
        )}
        self.assertIn("sane-utils", probes[vm.Capability.CORE])
        self.assertIn("sane-airscan", probes[vm.Capability.WSD])
        self.assertNotIn("hplip", probes[vm.Capability.WSD].lower())
        self.assertIn("libsane-hpaio", probes[vm.Capability.HPLIP])
        self.assertIn("bb_*.so", probes[vm.Capability.HP_PLUGIN])

    def test_failed_install_names_the_missing_component(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch("scanbox.vm._logged", return_value=False):
            with self.assertRaisesRegex(
                ui.ScanboxError, "component: wsd"
            ):
                vm.provision_capability(vm.Capability.WSD)


class ProvisioningScriptTests(unittest.TestCase):
    def _read(self, path: str) -> str:
        with open(path) as stream:
            return stream.read().lower()

    def test_wsd_scripts_do_not_install_or_initialize_hplip(self) -> None:
        combined = self._read(paths.PROVISION_CORE_SH) + self._read(
            paths.PROVISION_AIRSCAN_SH
        )
        commands = "\n".join(
            line for line in combined.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("hplip", commands)
        self.assertNotIn("hpaio", commands)
        self.assertNotIn("hp-plugin", commands)

    def test_backend_packages_are_split_across_scripts(self) -> None:
        self.assertNotIn("sane-airscan", self._read(paths.PROVISION_CORE_SH))
        self.assertIn("sane-airscan", self._read(paths.PROVISION_AIRSCAN_SH))
        self.assertIn("libsane-hpaio", self._read(paths.PROVISION_HPLIP_SH))
        self.assertIn("hp-plugin", self._read(paths.PROVISION_PLUGIN_SH))


if __name__ == "__main__":
    unittest.main()
