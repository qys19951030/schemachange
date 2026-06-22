"""
Tests for the out-of-order versioned script execution feature.

This feature allows versioned scripts to be applied even if their version number
is older than the max_published_version in the change history table. This is useful
for parallel development scenarios with timestamp-based versioning.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from unittest import mock

import pytest

from schemachange.config.DeployConfig import DeployConfig
from schemachange.deploy import deploy
from schemachange.version import get_alphanum_key, max_alphanumeric

# Minimal config for testing
minimal_deploy_config_kwargs: dict = {
    "snowflake_account": "test_account",
    "snowflake_user": "test_user",
    "snowflake_role": "test_role",
    "snowflake_warehouse": "test_warehouse",
}


class TestOutOfOrderConfig:
    """Test out_of_order configuration option."""

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    def test_out_of_order_defaults_to_false(self, _):
        """Test that out_of_order defaults to False for backward compatibility."""
        config = DeployConfig.factory(
            config_file_path=Path("."),
            **minimal_deploy_config_kwargs,
        )
        assert config.out_of_order is False

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    def test_out_of_order_can_be_enabled(self, _):
        """Test that out_of_order can be set to True."""
        config = DeployConfig.factory(
            config_file_path=Path("."),
            out_of_order=True,
            **minimal_deploy_config_kwargs,
        )
        assert config.out_of_order is True


class TestOutOfOrderDeployLogic:
    """Test the deploy logic with out_of_order enabled/disabled."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock SnowflakeSession."""
        session = mock.MagicMock()
        session.account = "test_account"
        session.role = "test_role"
        session.warehouse = "test_warehouse"
        session.database = "test_database"
        session.schema = "test_schema"
        session.change_history_table.fully_qualified = "METADATA.SCHEMACHANGE.CHANGE_HISTORY"
        return session

    @pytest.fixture
    def mock_config_base(self):
        """Create base config kwargs."""
        return {
            "config_file_path": Path("."),
            "root_folder": Path("."),
            "dry_run": False,
            "create_change_history_table": False,
            "raise_exception_on_ignored_versioned_script": False,
            "config_vars": {},
            "modules_folder": None,
            **minimal_deploy_config_kwargs,
        }

    def _create_mock_script(self, name: str, version: str, content: str = "SELECT 1;"):
        """Helper to create a mock versioned script."""
        script = mock.MagicMock()
        script.name = name
        script.version = version
        script.type = "V"
        script.format = "SQL"
        script.file_path = Path(f"/migrations/{name}")
        return script, content

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_out_of_order_disabled_skips_older_unapplied_scripts(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that with out_of_order=False (default), unapplied scripts older than
        max_published_version are skipped.

        Scenario: V1.0.3 has been applied (max_published_version = 1.0.3)
                  V1.0.2 is present but was never applied
                  Expected: V1.0.2 should be skipped
        """
        # Setup: V1.0.3 was applied, V1.0.2 was not
        script_v102, content_v102 = self._create_mock_script("v1.0.2__feature_a.sql", "1.0.2")
        mock_get_scripts.return_value = {"v1.0.2__feature_a.sql": script_v102}

        # Mock jinja processor
        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = content_v102
        mock_processor.relpath.return_value = "v1.0.2__feature_a.sql"
        mock_jinja.return_value = mock_processor

        # Mock session: V1.0.3 was applied (versioned_scripts doesn't contain v1.0.2)
        versioned_scripts = defaultdict(dict)  # V1.0.2 not in here = never applied
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.3")

        # Create config with out_of_order=False (default)
        config = DeployConfig.factory(out_of_order=False, **mock_config_base)

        # Run deploy
        deploy(config, mock_session)

        # Verify: apply_change_script should NOT be called (script was skipped)
        mock_session.apply_change_script.assert_not_called()

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_out_of_order_enabled_applies_older_unapplied_scripts(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that with out_of_order=True, unapplied scripts older than
        max_published_version are applied.

        Scenario: V1.0.3 has been applied (max_published_version = 1.0.3)
                  V1.0.2 is present but was never applied
                  Expected: V1.0.2 should be applied
        """
        # Setup: V1.0.3 was applied, V1.0.2 was not
        script_v102, content_v102 = self._create_mock_script("v1.0.2__feature_a.sql", "1.0.2")
        mock_get_scripts.return_value = {"v1.0.2__feature_a.sql": script_v102}

        # Mock jinja processor
        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = content_v102
        mock_processor.relpath.return_value = "v1.0.2__feature_a.sql"
        mock_jinja.return_value = mock_processor

        # Mock session: V1.0.3 was applied (versioned_scripts doesn't contain v1.0.2)
        versioned_scripts = defaultdict(dict)  # V1.0.2 not in here = never applied
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.3")

        # Create config with out_of_order=True
        config = DeployConfig.factory(out_of_order=True, **mock_config_base)

        # Run deploy
        deploy(config, mock_session)

        # Verify: apply_change_script SHOULD be called
        mock_session.apply_change_script.assert_called_once()
        call_args = mock_session.apply_change_script.call_args
        assert call_args.kwargs["script"] == script_v102

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_already_applied_scripts_always_skipped(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that scripts already recorded in change history are skipped
        regardless of out_of_order setting.
        """
        script_v102, content_v102 = self._create_mock_script("v1.0.2__feature_a.sql", "1.0.2")
        mock_get_scripts.return_value = {"v1.0.2__feature_a.sql": script_v102}

        # Mock jinja processor
        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = content_v102
        mock_processor.relpath.return_value = "v1.0.2__feature_a.sql"
        mock_jinja.return_value = mock_processor

        # Mock session: V1.0.2 was ALREADY applied
        checksum = hashlib.sha224(content_v102.encode("utf-8")).hexdigest()
        versioned_scripts = defaultdict(dict)
        versioned_scripts["v1.0.2__feature_a.sql"] = {
            "version": "1.0.2",
            "script": "v1.0.2__feature_a.sql",
            "checksum": checksum,
        }
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.3")

        # Create config with out_of_order=True
        config = DeployConfig.factory(out_of_order=True, **mock_config_base)

        # Run deploy
        deploy(config, mock_session)

        # Verify: apply_change_script should NOT be called (already applied)
        mock_session.apply_change_script.assert_not_called()

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_out_of_order_disabled_with_raise_exception_raises(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that with out_of_order=False and raise_exception_on_ignored=True,
        an exception is raised for unapplied older scripts.
        """
        script_v102, content_v102 = self._create_mock_script("v1.0.2__feature_a.sql", "1.0.2")
        mock_get_scripts.return_value = {"v1.0.2__feature_a.sql": script_v102}

        # Mock jinja processor
        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = content_v102
        mock_processor.relpath.return_value = "v1.0.2__feature_a.sql"
        mock_jinja.return_value = mock_processor

        # Mock session: V1.0.3 was applied, V1.0.2 was not
        versioned_scripts = defaultdict(dict)
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.3")

        # Create config with out_of_order=False and raise_exception=True
        mock_config_base["raise_exception_on_ignored_versioned_script"] = True
        config = DeployConfig.factory(out_of_order=False, **mock_config_base)

        # Run deploy - should raise
        with pytest.raises(ValueError) as exc_info:
            deploy(config, mock_session)

        assert "Versioned script will never be applied" in str(exc_info.value)
        assert "v1.0.2__feature_a.sql" in str(exc_info.value)

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_out_of_order_enabled_ignores_raise_exception_flag(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that with out_of_order=True, the raise_exception flag is irrelevant
        because the script gets applied instead of being ignored.
        """
        script_v102, content_v102 = self._create_mock_script("v1.0.2__feature_a.sql", "1.0.2")
        mock_get_scripts.return_value = {"v1.0.2__feature_a.sql": script_v102}

        # Mock jinja processor
        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = content_v102
        mock_processor.relpath.return_value = "v1.0.2__feature_a.sql"
        mock_jinja.return_value = mock_processor

        # Mock session: V1.0.3 was applied, V1.0.2 was not
        versioned_scripts = defaultdict(dict)
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.3")

        # Create config with out_of_order=True AND raise_exception=True
        mock_config_base["raise_exception_on_ignored_versioned_script"] = True
        config = DeployConfig.factory(out_of_order=True, **mock_config_base)

        # Run deploy - should NOT raise, should apply
        deploy(config, mock_session)

        # Verify: apply_change_script SHOULD be called
        mock_session.apply_change_script.assert_called_once()

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_multiple_out_of_order_scripts_applied(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that multiple out-of-order scripts are all applied.

        Scenario: V1.0.5 has been applied (max_published_version = 1.0.5)
                  V1.0.2, V1.0.3, V1.0.4 are present but never applied
                  Expected: All three should be applied in order
        """
        script_v102, content_v102 = self._create_mock_script("v1.0.2__feature_a.sql", "1.0.2")
        script_v103, content_v103 = self._create_mock_script("v1.0.3__feature_b.sql", "1.0.3")
        script_v104, content_v104 = self._create_mock_script("v1.0.4__feature_c.sql", "1.0.4")

        mock_get_scripts.return_value = {
            "v1.0.2__feature_a.sql": script_v102,
            "v1.0.3__feature_b.sql": script_v103,
            "v1.0.4__feature_c.sql": script_v104,
        }

        # Mock jinja processor to return appropriate content for each script
        mock_processor = mock.MagicMock()
        content_map = {
            "v1.0.2__feature_a.sql": content_v102,
            "v1.0.3__feature_b.sql": content_v103,
            "v1.0.4__feature_c.sql": content_v104,
        }
        mock_processor.render.side_effect = lambda path, _: content_map.get(path, "SELECT 1;")
        mock_processor.relpath.side_effect = lambda path: path.name
        mock_jinja.return_value = mock_processor

        # Mock session: V1.0.5 was applied, none of V1.0.2-V1.0.4 were
        versioned_scripts = defaultdict(dict)
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.5")

        # Create config with out_of_order=True
        config = DeployConfig.factory(out_of_order=True, **mock_config_base)

        # Run deploy
        deploy(config, mock_session)

        # Verify: apply_change_script should be called 3 times
        assert mock_session.apply_change_script.call_count == 3

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_newer_scripts_always_applied(self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base):
        """
        Test that scripts newer than max_published_version are always applied
        regardless of out_of_order setting.
        """
        script_v104, content_v104 = self._create_mock_script("v1.0.4__feature.sql", "1.0.4")
        mock_get_scripts.return_value = {"v1.0.4__feature.sql": script_v104}

        # Mock jinja processor
        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = content_v104
        mock_processor.relpath.return_value = "v1.0.4__feature.sql"
        mock_jinja.return_value = mock_processor

        # Mock session: V1.0.3 was applied
        versioned_scripts = defaultdict(dict)
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.3")

        # Test with out_of_order=False (default)
        config = DeployConfig.factory(out_of_order=False, **mock_config_base)

        # Run deploy
        deploy(config, mock_session)

        # Verify: apply_change_script SHOULD be called (newer version)
        mock_session.apply_change_script.assert_called_once()


class TestOutOfOrderCLI:
    """Test CLI argument parsing for out_of_order."""

    def test_out_of_order_cli_argument_parsed(self):
        """Test that --out-of-order CLI argument is parsed correctly."""
        from schemachange.config.parse_cli_args import parse_cli_args

        args = parse_cli_args(["deploy", "--out-of-order"])
        assert args.get("out_of_order") is True

    def test_out_of_order_cli_argument_absent(self):
        """Test that out_of_order is None when not provided."""
        from schemachange.config.parse_cli_args import parse_cli_args

        args = parse_cli_args(["deploy"])
        assert args.get("out_of_order") is None


class TestOutOfOrderEnvVar:
    """Test environment variable for out_of_order."""

    @mock.patch.dict("os.environ", {"SCHEMACHANGE_OUT_OF_ORDER": "true"})
    def test_out_of_order_env_var_true(self):
        """Test that SCHEMACHANGE_OUT_OF_ORDER=true is parsed correctly."""
        from schemachange.config.utils import get_schemachange_config_from_env

        env_config = get_schemachange_config_from_env()
        assert env_config.get("out_of_order") is True

    @mock.patch.dict("os.environ", {"SCHEMACHANGE_OUT_OF_ORDER": "false"})
    def test_out_of_order_env_var_false(self):
        """Test that SCHEMACHANGE_OUT_OF_ORDER=false is parsed correctly."""
        from schemachange.config.utils import get_schemachange_config_from_env

        env_config = get_schemachange_config_from_env()
        assert env_config.get("out_of_order") is False

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_out_of_order_env_var_absent(self):
        """Test that out_of_order is not present when env var is not set."""
        from schemachange.config.utils import get_schemachange_config_from_env

        env_config = get_schemachange_config_from_env()
        assert "out_of_order" not in env_config


class TestStrictChecksumDriftConfig:
    """Test strict_checksum_drift configuration option."""

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    def test_strict_checksum_drift_defaults_to_false(self, _):
        """Test that strict_checksum_drift defaults to False for backward compatibility."""
        config = DeployConfig.factory(
            config_file_path=Path("."),
            **minimal_deploy_config_kwargs,
        )
        assert config.strict_checksum_drift is False

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    def test_strict_checksum_drift_can_be_enabled(self, _):
        """Test that strict_checksum_drift can be set to True."""
        config = DeployConfig.factory(
            config_file_path=Path("."),
            strict_checksum_drift=True,
            **minimal_deploy_config_kwargs,
        )
        assert config.strict_checksum_drift is True


class TestStrictChecksumDriftDeployLogic:
    """Test the deploy logic with strict_checksum_drift enabled/disabled."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock SnowflakeSession."""
        session = mock.MagicMock()
        session.account = "test_account"
        session.role = "test_role"
        session.warehouse = "test_warehouse"
        session.database = "test_database"
        session.schema = "test_schema"
        session.change_history_table.fully_qualified = "METADATA.SCHEMACHANGE.CHANGE_HISTORY"
        return session

    @pytest.fixture
    def mock_config_base(self):
        """Create base config kwargs."""
        return {
            "config_file_path": Path("."),
            "root_folder": Path("."),
            "dry_run": False,
            "create_change_history_table": False,
            "raise_exception_on_ignored_versioned_script": False,
            "out_of_order": False,
            "config_vars": {},
            "modules_folder": None,
            **minimal_deploy_config_kwargs,
        }

    def _create_mock_script(self, name: str, version: str, content: str = "SELECT 1;"):
        """Helper to create a mock versioned script."""
        script = mock.MagicMock()
        script.name = name
        script.version = version
        script.type = "V"
        script.format = "SQL"
        script.file_path = Path(f"/migrations/{name}")
        return script, content

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_default_continues_on_checksum_drift(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that by default (strict_checksum_drift=False), deploy continues
        when a versioned script's checksum has drifted from what's in change history.
        """
        script_name = "v1.0.0__initial.sql"
        script_v100, original_content = self._create_mock_script(script_name, "1.0.0")
        modified_content = "SELECT 2; -- modified"
        mock_get_scripts.return_value = {script_name: script_v100}

        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = modified_content
        mock_processor.relpath.return_value = script_name
        mock_processor.prepare_for_execution.return_value = modified_content
        mock_jinja.return_value = mock_processor

        original_checksum = hashlib.sha224(original_content.encode("utf-8")).hexdigest()
        versioned_scripts = defaultdict(dict)
        versioned_scripts[script_name] = {
            "version": "1.0.0",
            "script": script_name,
            "checksum": original_checksum,
        }
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.0")

        config = DeployConfig.factory(strict_checksum_drift=False, **mock_config_base)

        deploy(config, mock_session)

        mock_session.apply_change_script.assert_not_called()

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_strict_mode_raises_on_checksum_drift(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that with strict_checksum_drift=True, deploy raises a ValueError
        when a versioned script's checksum has drifted from what's in change history.
        """
        script_name = "v1.0.0__initial.sql"
        script_v100, original_content = self._create_mock_script(script_name, "1.0.0")
        modified_content = "SELECT 2; -- modified"
        mock_get_scripts.return_value = {script_name: script_v100}

        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = modified_content
        mock_processor.relpath.return_value = script_name
        mock_jinja.return_value = mock_processor

        original_checksum = hashlib.sha224(original_content.encode("utf-8")).hexdigest()
        versioned_scripts = defaultdict(dict)
        versioned_scripts[script_name] = {
            "version": "1.0.0",
            "script": script_name,
            "checksum": original_checksum,
        }
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.0")

        config = DeployConfig.factory(strict_checksum_drift=True, **mock_config_base)

        with pytest.raises(ValueError) as exc_info:
            deploy(config, mock_session)

        assert "Versioned script checksum has drifted since application" in str(exc_info.value)
        assert script_name in str(exc_info.value)
        assert original_checksum in str(exc_info.value)

        current_checksum = hashlib.sha224(modified_content.encode("utf-8")).hexdigest()
        assert current_checksum in str(exc_info.value)

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_strict_mode_no_drift_no_error(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that with strict_checksum_drift=True, deploy does NOT raise
        when the versioned script's checksum matches what's in change history.
        """
        script_name = "v1.0.0__initial.sql"
        script_v100, content = self._create_mock_script(script_name, "1.0.0")
        mock_get_scripts.return_value = {script_name: script_v100}

        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = content
        mock_processor.relpath.return_value = script_name
        mock_jinja.return_value = mock_processor

        checksum = hashlib.sha224(content.encode("utf-8")).hexdigest()
        versioned_scripts = defaultdict(dict)
        versioned_scripts[script_name] = {
            "version": "1.0.0",
            "script": script_name,
            "checksum": checksum,
        }
        mock_session.get_script_metadata.return_value = (versioned_scripts, None, "1.0.0")

        config = DeployConfig.factory(strict_checksum_drift=True, **mock_config_base)

        deploy(config, mock_session)

        mock_session.apply_change_script.assert_not_called()

    @mock.patch("pathlib.Path.is_dir", return_value=True)
    @mock.patch("schemachange.deploy.get_all_scripts_recursively")
    @mock.patch("schemachange.deploy.JinjaTemplateProcessor")
    def test_strict_mode_does_not_affect_repeatable_scripts(
        self, mock_jinja, mock_get_scripts, _, mock_session, mock_config_base
    ):
        """
        Test that strict_checksum_drift does NOT affect repeatable (R) scripts.
        R scripts should still be re-executed when their checksum changes.
        """
        r_script_name = "r__my_view.sql"
        r_script = mock.MagicMock()
        r_script.name = r_script_name
        r_script.type = "R"
        r_script.format = "SQL"
        r_script.file_path = Path(f"/migrations/{r_script_name}")

        mock_get_scripts.return_value = {r_script_name: r_script}

        current_content = "CREATE OR REPLACE VIEW my_view AS SELECT 2;"
        mock_processor = mock.MagicMock()
        mock_processor.render.return_value = current_content
        mock_processor.relpath.return_value = r_script_name
        mock_processor.prepare_for_execution.return_value = current_content
        mock_jinja.return_value = mock_processor

        last_checksum = hashlib.sha224("CREATE OR REPLACE VIEW my_view AS SELECT 1;".encode("utf-8")).hexdigest()
        r_scripts_checksum = {r_script_name: (last_checksum, None)}
        mock_session.get_script_metadata.return_value = ({}, r_scripts_checksum, None)

        config = DeployConfig.factory(strict_checksum_drift=True, **mock_config_base)

        deploy(config, mock_session)

        mock_session.apply_change_script.assert_called_once()


class TestStrictChecksumDriftEnvVar:
    """Test environment variable for strict_checksum_drift."""

    @mock.patch.dict("os.environ", {"SCHEMACHANGE_STRICT_CHECKSUM_DRIFT": "true"})
    def test_strict_checksum_drift_env_var_true(self):
        """Test that SCHEMACHANGE_STRICT_CHECKSUM_DRIFT=true is parsed correctly."""
        from schemachange.config.utils import get_schemachange_config_from_env

        env_config = get_schemachange_config_from_env()
        assert env_config.get("strict_checksum_drift") is True

    @mock.patch.dict("os.environ", {"SCHEMACHANGE_STRICT_CHECKSUM_DRIFT": "false"})
    def test_strict_checksum_drift_env_var_false(self):
        """Test that SCHEMACHANGE_STRICT_CHECKSUM_DRIFT=false is parsed correctly."""
        from schemachange.config.utils import get_schemachange_config_from_env

        env_config = get_schemachange_config_from_env()
        assert env_config.get("strict_checksum_drift") is False

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_strict_checksum_drift_env_var_absent(self):
        """Test that strict_checksum_drift is not present when env var is not set."""
        from schemachange.config.utils import get_schemachange_config_from_env

        env_config = get_schemachange_config_from_env()
        assert "strict_checksum_drift" not in env_config


class TestAlphanumKey:
    """Test get_alphanum_key function used for version comparison."""

    def test_simple_version(self):
        """Test simple version number."""
        assert get_alphanum_key("1.0.0") == ["", 1, ".", 0, ".", 0, ""]

    def test_timestamp_version(self):
        """Test timestamp-based version number."""
        key = get_alphanum_key("20260122143052")
        assert key == ["", 20260122143052, ""]

    def test_version_comparison(self):
        """Test that version comparison works correctly."""
        v100 = get_alphanum_key("1.0.0")
        v101 = get_alphanum_key("1.0.1")
        v110 = get_alphanum_key("1.1.0")

        assert v100 < v101
        assert v101 < v110
        assert v100 < v110

    def test_timestamp_comparison(self):
        """Test that timestamp comparison works correctly."""
        t1 = get_alphanum_key("20260122143052")
        t2 = get_alphanum_key("20260122143053")
        t3 = get_alphanum_key("20260123000000")

        assert t1 < t2
        assert t2 < t3


class TestMaxAlphanumeric:
    """Test max_alphanumeric function used to find the highest version."""

    def test_simple_versions(self):
        """Test finding max among simple versions."""
        versions = ["1.0.0", "1.0.1", "1.1.0", "2.0.0"]
        assert max_alphanumeric(versions) == "2.0.0"

    def test_semantic_versioning_with_double_digits(self):
        """Test that 1.0.10 > 1.0.2 (not string comparison)."""
        versions = ["1.0.2", "1.0.10", "1.0.1"]
        assert max_alphanumeric(versions) == "1.0.10"

    def test_out_of_order_versions(self):
        """Test finding max when versions were applied out of order."""
        # Simulates: V1.0.3 applied first, V1.0.2 applied second (out of order)
        versions = ["1.0.3", "1.0.2"]
        assert max_alphanumeric(versions) == "1.0.3"

    def test_timestamp_versions(self):
        """Test finding max among timestamp-based versions."""
        versions = ["20260122143052", "20260121000000", "20260122154512"]
        assert max_alphanumeric(versions) == "20260122154512"

    def test_empty_list(self):
        """Test with empty list."""
        assert max_alphanumeric([]) is None

    def test_list_with_none_values(self):
        """Test with None values in list."""
        versions = [None, "1.0.0", None, "1.0.1"]
        assert max_alphanumeric(versions) == "1.0.1"

    def test_list_with_empty_strings(self):
        """Test with empty strings in list."""
        versions = ["", "1.0.0", "", "1.0.1"]
        assert max_alphanumeric(versions) == "1.0.1"

    def test_list_with_only_none(self):
        """Test with only None values."""
        versions = [None, None]
        assert max_alphanumeric(versions) is None

    def test_mixed_version_styles(self):
        """Test with mixed version styles."""
        # Timestamp-based versions sort higher than semantic versions
        versions = ["1.0.0", "20260122143052", "2.0.0"]
        result = max_alphanumeric(versions)
        # Timestamp should be highest due to the large number
        assert result == "20260122143052"
