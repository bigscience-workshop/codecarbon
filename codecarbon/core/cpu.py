"""
Implements tracking Intel CPU Power Consumption on Mac and Windows
using Intel Power Gadget https://software.intel.com/content/www/us/en/develop/articles/intel-power-gadget.html
"""
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, Union

import cpuinfo
import pandas as pd
from fuzzywuzzy import fuzz

from codecarbon.core.rapl import RAPLFile
from codecarbon.external.logger import logger
from codecarbon.input import DataSource


def is_powergadget_available():
    try:
        IntelPowerGadget()
        return True
    except Exception as e:
        logger.debug(
            f"Exception occurred while instantiating IntelPowerGadget : {e}",
            exc_info=True,
        )
        return False


def is_rapl_available():
    try:
        IntelRAPL()
        return True
    except Exception as e:
        logger.debug(
            f"Exception occurred while instantiating RAPLInterface : {e}",
            exc_info=True,
        )
        return False


class IntelPowerGadget:
    _osx_exec = "PowerLog"
    _osx_exec_backup = "/Applications/Intel Power Gadget/PowerLog"
    _windows_exec = "PowerLog3.0.exe"
    _windows_exec_backup = "C:\\Program Files\\Intel\\Power Gadget 3.5\\PowerLog3.0.exe"

    def __init__(
        self,
        output_dir: str = ".",
        duration=1,
        resolution=100,
        log_file_name="intel_power_gadget_log.csv",
    ):
        self._log_file_path = os.path.join(output_dir, log_file_name)
        self._system = sys.platform.lower()
        self._duration = duration
        self._resolution = resolution
        self._setup_cli()

    def _setup_cli(self):
        """
        Setup cli command to run Intel Power Gadget
        """
        if self._system.startswith("win"):
            if shutil.which(self._windows_exec):
                self._cli = shutil.which(
                    self._windows_exec
                )  # Windows exec is a relative path
            elif shutil.which(self._windows_exec_backup):
                self._cli = self._windows_exec_backup
            else:
                raise FileNotFoundError(
                    f"Intel Power Gadget executable not found on {self._system}"
                )
        elif self._system.startswith("darwin"):
            if shutil.which(self._osx_exec):
                self._cli = self._osx_exec
            elif shutil.which(self._osx_exec_backup):
                self._cli = self._osx_exec_backup
            else:
                raise FileNotFoundError(
                    f"Intel Power Gadget executable not found on {self._system}"
                )
        else:
            raise SystemError("Platform not supported by Intel Power Gadget")

    def _log_values(self):
        """
        Logs output from Intel Power Gadget command line to a file
        """
        returncode = None
        if self._system.startswith("win"):
            returncode = subprocess.call(
                [
                    self._cli,
                    "-duration",
                    str(self._duration),
                    "-resolution",
                    str(self._resolution),
                    "-file",
                    self._log_file_path,
                ],
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        elif self._system.startswith("darwin"):
            returncode = subprocess.call(
                f"'{self._cli}' -duration {self._duration} -resolution {self._resolution} -file {self._log_file_path} > /dev/null",
                shell=True,
            )
        else:
            return None

        if returncode != 0:
            logger.warning(
                "Returncode while logging power values using "
                + f"Intel Power Gadget: {returncode}"
            )
        return

    def get_cpu_details(self) -> Dict:
        """
        Fetches the CPU Power Details by fetching values from a logged csv file in _log_values function
        """
        self._log_values()
        cpu_details = dict()
        try:
            cpu_data = pd.read_csv(self._log_file_path).dropna()
            for col_name in cpu_data.columns:
                if col_name in ["System Time", "Elapsed Time (sec)", "RDTSC"]:
                    continue
                if "Cumulative" in col_name:
                    cpu_details[col_name] = cpu_data[col_name].iloc[-1]
                else:
                    cpu_details[col_name] = cpu_data[col_name].mean()
        except Exception as e:
            logger.info(
                f"Unable to read Intel Power Gadget logged file at {self._log_file_path}\n \
                Exception occurred {e}",
                exc_info=True,
            )
        return cpu_details


class IntelRAPL:
    def __init__(self, rapl_dir="/sys/class/powercap/intel-rapl"):
        self._lin_rapl_dir = rapl_dir
        self._system = sys.platform.lower()
        self._delay = 0.01  # 10 millisecond
        self._rapl_files = list()
        self._setup_rapl()

    def _is_platform_supported(self) -> bool:
        return self._system.startswith("lin")

    def _setup_rapl(self):
        if self._is_platform_supported():
            if os.path.exists(self._lin_rapl_dir):
                self._fetch_rapl_files()
            else:
                raise FileNotFoundError(
                    f"Intel RAPL files not found at {self._lin_rapl_dir} on {self._system}"
                )
        else:
            raise SystemError("Platform not supported by Intel RAPL Interface")
        return

    def _fetch_rapl_files(self):
        """
        Fetches RAPL files from the RAPL directory
        """

        # consider files like `intel-rapl:$i`
        files = list(filter(lambda x: ":" in x, os.listdir(self._lin_rapl_dir)))

        i = 0
        for file in files:
            path = os.path.join(self._lin_rapl_dir, file, "name")
            with open(path) as f:
                name = f.read().strip()
                if "package" in name:
                    name = f"Processor Power_{i}(Watt)"
                    i += 1
                self._rapl_files.append(
                    RAPLFile(name, os.path.join(self._lin_rapl_dir, file, "energy_uj"))
                )
        return

    def get_cpu_details(self) -> Dict:
        """
        Fetches the CPU Power Details by fetching values from RAPL files
        """
        cpu_details = dict()
        try:
            list(map(lambda rapl_file: rapl_file.start(), self._rapl_files))
            time.sleep(self._delay)
            list(map(lambda rapl_file: rapl_file.end(self._delay), self._rapl_files))
            for rapl_file in self._rapl_files:
                cpu_details[rapl_file.name] = rapl_file.power_measurement
        except Exception as e:
            logger.info(
                f"Unable to read Intel RAPL files at {self._rapl_files}\n \
                Exception occurred {e}",
                exc_info=True,
            )
        return cpu_details


class TDP:
    def __init__(self):
        self.model, self.tdp = self._main()

    @staticmethod
    def _detect_cpu_model() -> str:
        cpu_info = cpuinfo.get_cpu_info()
        if cpu_info:
            cpu_model_detected = cpu_info.get("brand_raw", "")
            return cpu_model_detected
        else:
            return None

    @staticmethod
    def _get_cpu_constant_power(match: str, cpu_power_df: pd.DataFrame) -> int:
        """Extract constant power from matched CPU"""
        return cpu_power_df[cpu_power_df["Name"] == match]["TDP"].values[0]

    def _get_cpu_power_from_registry(self, cpu_model_raw: str) -> int:
        cpu_power_df = DataSource().get_cpu_power_data()
        cpu_matching = self._get_matching_cpu(cpu_model_raw, cpu_power_df)
        if cpu_matching:
            power = self._get_cpu_constant_power(cpu_matching, cpu_power_df)
            return power
        else:
            return None

    @staticmethod
    def _get_cpus(cpu_df, cpu_idxs) -> list:
        return [cpu_df["Name"][idx] for idx in cpu_idxs]

    @staticmethod
    def _get_direct_matches(moodel: str, cpu_df: pd.DataFrame) -> list:
        model_l = moodel.lower()
        return [fuzz.ratio(model_l, cpu.lower()) for cpu in cpu_df["Name"]]

    @staticmethod
    def _get_token_set_matches(model: str, cpu_df: pd.DataFrame) -> list:
        return [fuzz.token_set_ratio(model, cpu) for cpu in cpu_df["Name"]]

    @staticmethod
    def _get_single_direct_match(
        ratios: list, max_ratio: int, cpu_df: pd.DataFrame
    ) -> str:
        idx = ratios.index(max_ratio)
        cpu_matched = cpu_df["Name"].iloc[idx]
        return cpu_matched

    def _get_matching_cpu(
        self, model_raw: str, cpu_df: pd.DataFrame, greedy=False
    ) -> str:
        """
        Get matching cpu name

        :args:
            model_raw (str): raw name of the cpu model detected on the machine

            cpu_df (DataFrame): table containing cpu models along their tdp

            greedy (default False): if multiple cpu models match with an equal
            ratio of similarity, greedy (True) selects the first model,
            following the order of the cpu list provided, while non-greedy
            returns None.

        :return: name of the matching cpu model

        :notes:
            Thanks to the greedy mode, even though the match could be a model
            with a tdp very different from the actual tdp of current cpu, it
            still enables the relative comparison of models emissions running
            on the same machine.

            THRESHOLD_DIRECT defines the similiraty ratio value to consider
            almost-exact matches.

            THRESHOLD_TOKEN_SET defines the similarity ratio value to consider
            token_set matches (for more detail see fuzz.token_set_ratio).
        """
        THRESHOLD_DIRECT = 100
        THRESHOLD_TOKEN_SET = 100

        ratios_direct = self._get_direct_matches(model_raw, cpu_df)
        ratios_token_set = self._get_token_set_matches(model_raw, cpu_df)
        max_ratio_direct = max(ratios_direct)
        max_ratio_token_set = max(ratios_token_set)

        # Check if a direct match exists
        if max_ratio_direct >= THRESHOLD_DIRECT:
            cpu_matched = self._get_single_direct_match(
                ratios_direct, max_ratio_direct, cpu_df
            )
            return cpu_matched

        # Check if an indirect match exists
        if max_ratio_token_set < THRESHOLD_TOKEN_SET:
            return None
        else:
            cpu_idxs = self._get_max_idxs(ratios_token_set, max_ratio_token_set)
            cpu_machings = self._get_cpus(cpu_df, cpu_idxs)

            if (cpu_machings and len(cpu_machings) == 1) or greedy:
                cpu_matched = cpu_machings[0]
                return cpu_matched
            else:
                return None

    @staticmethod
    def _get_max_idxs(ratios: list, max_ratio: int) -> list:
        return [idx for idx, ratio in enumerate(ratios) if ratio == max_ratio]

    def _main(self) -> Union[str, int]:
        """
        Get CPU power from constant mode

        :return: model name (str), power in Watt (int)
        """
        cpu_model_detected = self._detect_cpu_model()

        if cpu_model_detected:
            power = self._get_cpu_power_from_registry(cpu_model_detected)

            if power:
                logger.debug(
                    f"CPU : We detect a {cpu_model_detected} with a TDP of {power} W"
                )
                return cpu_model_detected, power
            else:
                logger.warning(
                    f"We saw that you have a {cpu_model_detected} but we don't know it."
                    + " Please contact us."
                )
                return cpu_model_detected, None
        else:
            logger.warning(
                "We were unable to detect your CPU using the `cpuinfo` package."
                + " Resorting to a default power consumption of 85W."
            )
        return "Unknown", None
