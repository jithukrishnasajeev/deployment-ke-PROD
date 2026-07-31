"""
Unified Deployment Pipeline Engine.
Implements 3-step orchestration (Download -> Upload & Extract -> Deploy)
used by both CLI and Web interface with complete feature fidelity.
"""

import os
import sys
import time
import threading
import concurrent.futures
from typing import Callable, List, Dict, Optional, Tuple

from .config import DeploymentConfig
from .ssh import SSHClient, fast_download
from .s3 import S3Client
from .utils import calculate_local_md5, get_remote_md5, format_size


class DeploymentEngine:
    """Core orchestration engine for managing deployments."""

    def __init__(self, config: DeploymentConfig = None):
        self.config = config or DeploymentConfig()
        self.is_cancelled = False
        self.failed_wars: List[str] = []
        self.failed_routes: Dict[str, int] = {}
        self._print_lock = threading.Lock()

    def cancel(self):
        """Cancel ongoing deployment execution."""
        self.is_cancelled = True

    def check_cancelled(self) -> bool:
        """Return true if cancellation requested."""
        return self.is_cancelled

    def run_step1_download(
        self,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[str, str, int, int], None]] = None
    ) -> List[str]:
        """Step 1: Download WAR / artifact files to local download folder (Direct WAR or Tar mode)."""
        def log(msg: str):
            if log_callback:
                log_callback(msg)
            else:
                try:
                    print(msg)
                except UnicodeEncodeError:
                    print(msg.encode('ascii', errors='replace').decode('ascii'))

        log("\n" + "=" * 60)
        log(f"STEP 1: Download Artifacts to Local [{self.config.DOWNLOAD_SOURCE.upper()} Source]")
        log("=" * 60 + "\n")

        os.makedirs(self.config.LOCAL_DOWNLOAD_PATH, exist_ok=True)
        downloaded_files = []
        source_type = self.config.DOWNLOAD_SOURCE.lower()

        if source_type == 's3':
            # AWS S3 Cloud Source
            s3_client = S3Client(
                bucket=self.config.S3_BUCKET,
                profile=self.config.S3_PROFILE,
                region=self.config.S3_REGION
            )

            for idx, (war_prefix, deploy_folder) in enumerate(self.config.WAR_MAPPINGS, 1):
                if self.check_cancelled():
                    log("[INFO] Download cancelled by user.")
                    break

                war_file = f"{war_prefix}-{self.config.VERSION}.war"
                local_path = os.path.join(self.config.LOCAL_DOWNLOAD_PATH, war_file)
                prefix_tpl = self.config.S3_PREFIX_TEMPLATE.format(version=self.config.VERSION)
                s3_key = f"{prefix_tpl.rstrip('/')}/{war_file}"

                log(f"[{idx}/{len(self.config.WAR_MAPPINGS)}] S3 Download: {war_file}...")
                start_t = time.time()
                try:
                    def _cb(trans, tot):
                        if progress_callback:
                            progress_callback(war_prefix, war_file, trans, tot)

                    md5, file_size = s3_client.download_file(
                        s3_key, local_path,
                        progress_callback=_cb,
                        cancel_check=self.check_cancelled
                    )
                    elapsed = max(0.1, time.time() - start_t)
                    speed_mb = (file_size / (1024 * 1024)) / elapsed
                    log(f"  [SUCCESS] Saved {war_file} ({format_size(file_size)} @ {speed_mb:.2f} MB/s) [MD5: {md5[:8]}...]")
                    downloaded_files.append(local_path)
                except Exception as e:
                    log(f"  [ERROR] Failed to download {war_file} from S3: {e}")

        else:
            # SSH Source Server
            source_wars_dir = f"{self.config.SOURCE_PATH}Wars"
            parallel = self.config.PARALLEL_DOWNLOADS
            max_workers = self.config.MAX_THREADS if parallel else 1
            direct_dl = self.config.DIRECT_WAR_DOWNLOAD

            log(f"[INFO] Download Mode: {'Direct WAR' if direct_dl else 'Tar Packaging'}")
            log(f"[INFO] Threads:       {'Parallel (' + str(max_workers) + ' threads)' if parallel else 'Single SSH Session'}\n")

            def _download_single_item(item):
                if self.check_cancelled():
                    return None

                idx, (war_prefix, deploy_folder) = item
                war_file = f"{war_prefix}-{self.config.VERSION}.war"
                tar_file = f"{war_prefix}-{self.config.VERSION}.tar"

                ssh = SSHClient(
                    hostname=self.config.SOURCE_SERVER,
                    username=self.config.SOURCE_USER,
                    password=self.config.SOURCE_PASSWORD,
                    port=self.config.SOURCE_PORT
                )
                ssh.connect()
                try:
                    sftp = ssh.get_sftp()

                    if direct_dl:
                        remote_path = f"{source_wars_dir}/{war_file}"
                        local_path = os.path.join(self.config.LOCAL_DOWNLOAD_PATH, war_file)

                        remote_md5 = get_remote_md5(ssh, remote_path)
                        start_t = time.time()

                        def _cb(transferred, total):
                            if progress_callback:
                                progress_callback(war_prefix, war_file, transferred, total)

                        local_md5, file_size = fast_download(
                            ssh, sftp, remote_path, local_path,
                            progress_callback=_cb,
                            cancel_check=self.check_cancelled,
                            use_scp=self.config.USE_SCP
                        )
                        elapsed = max(0.1, time.time() - start_t)
                        speed_mb = (file_size / (1024 * 1024)) / elapsed

                        with self._print_lock:
                            if remote_md5 and local_md5 == remote_md5:
                                log(f"  [SUCCESS] [{idx}/{len(self.config.WAR_MAPPINGS)}] {war_file} ({format_size(file_size)} @ {speed_mb:.2f} MB/s) - Verified MD5")
                            else:
                                log(f"  [WARNING] [{idx}/{len(self.config.WAR_MAPPINGS)}] {war_file} downloaded ({format_size(file_size)}) - MD5 check warning")
                        return local_path
                    else:
                        # Tar packaging mode
                        remote_tar = f"/tmp/{tar_file}"
                        local_path = os.path.join(self.config.LOCAL_DOWNLOAD_PATH, tar_file)

                        with self._print_lock:
                            log(f"  [{idx}/{len(self.config.WAR_MAPPINGS)}] Packaging + downloading: {tar_file}")

                        out, err, code = ssh.execute_command(f"cd '{source_wars_dir}' && tar -cf '{remote_tar}' '{war_file}'")
                        if code != 0:
                            with self._print_lock:
                                log(f"  [ERROR] Packaging failed for {war_file}: {err}")
                            return None

                        remote_md5 = get_remote_md5(ssh, remote_tar)
                        local_md5, file_size = fast_download(
                            ssh, sftp, remote_tar, local_path,
                            progress_callback=lambda t, s: progress_callback(war_prefix, tar_file, t, s) if progress_callback else None,
                            cancel_check=self.check_cancelled,
                            use_scp=self.config.USE_SCP
                        )
                        ssh.execute_command(f"rm -f '{remote_tar}'")

                        with self._print_lock:
                            if remote_md5 and local_md5 == remote_md5:
                                log(f"  [SUCCESS] Downloaded & Verified {tar_file}")
                            else:
                                log(f"  [WARNING] Checksum mismatch for {tar_file}")
                        return local_path

                except Exception as e:
                    with self._print_lock:
                        log(f"  [ERROR] [{idx}] Failed {war_file}: {e}")
                    return None
                finally:
                    ssh.close()

            items = list(enumerate(self.config.WAR_MAPPINGS, 1))
            if parallel and max_workers > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    results = list(executor.map(_download_single_item, items))
                downloaded_files = [r for r in results if r]
            else:
                for item in items:
                    res = _download_single_item(item)
                    if res:
                        downloaded_files.append(res)

        log("\n" + "=" * 60)
        log(f"[SUCCESS] Step 1 Complete! {len(downloaded_files)}/{len(self.config.WAR_MAPPINGS)} items stored in {self.config.LOCAL_DOWNLOAD_PATH}")
        log("=" * 60 + "\n")
        return downloaded_files

    def run_step2_upload(
        self,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[str, str, int, int], None]] = None,
        target_war_prefixes: Optional[List[str]] = None
    ) -> bool:
        """Step 2: Upload local artifacts (.war, .zip, .tar) to remote Target Server utilities path and extract."""
        def log(msg: str):
            if log_callback:
                log_callback(msg)
            else:
                try:
                    print(msg)
                except UnicodeEncodeError:
                    print(msg.encode('ascii', errors='replace').decode('ascii'))

        log("\n" + "=" * 60)
        log("STEP 2: Upload & Extract Artifacts to Target Server")
        log("=" * 60 + "\n")

        routes = self.config.TARGET_ROUTES if self.config.TARGET_ROUTES else [{'host': self.config.TARGET_SERVER, 'username': self.config.TARGET_USER}]
        target_route = routes[0]

        ssh = SSHClient(
            hostname=target_route['host'],
            username=target_route['username'],
            password=self.config.TARGET_PASSWORD,
            port=self.config.TARGET_PORT
        )
        ssh.connect()

        try:
            log(f"[INFO] Creating target utilities directory: {self.config.TARGET_EXTRACT_PATH}")
            ssh.execute_command(f"mkdir -p '{self.config.TARGET_EXTRACT_PATH}'")

            mappings_to_process = self.config.WAR_MAPPINGS
            if target_war_prefixes:
                mappings_to_process = [m for m in self.config.WAR_MAPPINGS if m[0] in target_war_prefixes]

            max_workers = min(5, max(1, len(mappings_to_process)))
            log(f"[INFO] Uploading across routes with {max_workers} threads...\n")

            self.failed_wars.clear()
            self.failed_routes.clear()

            def _upload_item(item):
                if self.check_cancelled():
                    return False

                idx, (war_prefix, deploy_folder) = item
                war_file = f"{war_prefix}-{self.config.VERSION}.war"
                zip_file = f"{war_prefix}-{self.config.VERSION}.zip"
                war_zip_file = f"{war_prefix}-{self.config.VERSION}.war.zip"
                tar_file = f"{war_prefix}-{self.config.VERSION}.tar"

                candidates = [
                    os.path.join(self.config.LOCAL_DOWNLOAD_PATH, war_file),
                    os.path.join(self.config.LOCAL_DOWNLOAD_PATH, zip_file),
                    os.path.join(self.config.LOCAL_DOWNLOAD_PATH, war_zip_file),
                    os.path.join(self.config.LOCAL_DOWNLOAD_PATH, tar_file),
                ]

                local_file = None
                for candidate in candidates:
                    if os.path.exists(candidate):
                        local_file = candidate
                        break

                if not local_file:
                    with self._print_lock:
                        log(f"  [ERROR] [{idx}] Local file missing for {war_prefix}")
                        self.failed_wars.append(war_prefix)
                    return False

                filename = os.path.basename(local_file)
                file_size = os.path.getsize(local_file)
                route_idx = (idx - 1) % len(routes)
                route = routes[route_idx]

                item_ssh = SSHClient(
                    hostname=route['host'],
                    username=route['username'],
                    password=self.config.TARGET_PASSWORD,
                    port=self.config.TARGET_PORT
                )
                item_ssh.connect()
                try:
                    sftp = item_ssh.get_sftp()

                    def _cb(transferred, total):
                        if progress_callback:
                            progress_callback(war_prefix, filename, transferred, total)

                    if filename.endswith('.war'):
                        # Direct WAR upload
                        remote_dest = f"{self.config.TARGET_EXTRACT_PATH}/{war_file}"
                        sftp.put(local_file, remote_dest, callback=_cb, confirm=False)
                        local_md5 = calculate_local_md5(local_file)
                        remote_md5 = get_remote_md5(item_ssh, remote_dest)

                        with self._print_lock:
                            if remote_md5 and local_md5 != remote_md5:
                                log(f"  [WARNING] [{idx}] MD5 mismatch for {war_file}")
                            else:
                                log(f"  [SUCCESS] [{idx}/{len(mappings_to_process)}] Uploaded direct WAR {war_file} via {route['host']}")
                        return True

                    elif filename.endswith('.zip'):
                        # Upload ZIP and unzip on remote target
                        tmp_zip = f"/tmp/{filename}_{threading.current_thread().ident}"
                        sftp.put(local_file, tmp_zip, callback=_cb, confirm=False)
                        local_md5 = calculate_local_md5(local_file)
                        remote_md5 = get_remote_md5(item_ssh, tmp_zip)

                        if remote_md5 and local_md5 != remote_md5:
                            log(f"  [WARNING] [{idx}] MD5 mismatch for {filename}")

                        log(f"  [{idx}] Unzipping {filename} on target server...")
                        unzip_cmd = (
                            f"unzip -o -q '{tmp_zip}' -d '{self.config.TARGET_EXTRACT_PATH}' || "
                            f"python3 -m zipfile -e '{tmp_zip}' '{self.config.TARGET_EXTRACT_PATH}'"
                        )
                        out, err, code = item_ssh.execute_command(unzip_cmd)
                        item_ssh.execute_command(f"rm -f '{tmp_zip}'")

                        if code != 0:
                            with self._print_lock:
                                log(f"  [ERROR] [{idx}] Unzip failed for {filename}: {err}")
                                self.failed_wars.append(war_prefix)
                                self.failed_routes[war_prefix] = route_idx
                            return False

                        with self._print_lock:
                            log(f"  [SUCCESS] [{idx}/{len(mappings_to_process)}] Uploaded & unzipped {filename}")
                        return True

                    else:
                        # Upload TAR file to /tmp and extract
                        tmp_tar = f"/tmp/{filename}_{threading.current_thread().ident}"
                        sftp.put(local_file, tmp_tar, callback=_cb, confirm=False)
                        local_md5 = calculate_local_md5(local_file)
                        remote_md5 = get_remote_md5(item_ssh, tmp_tar)

                        if remote_md5 and local_md5 != remote_md5:
                            log(f"  [WARNING] [{idx}] MD5 mismatch for {filename}")

                        log(f"  [{idx}] Extracting tar {filename} on target server...")
                        out, err, code = item_ssh.execute_command(f"cd '{self.config.TARGET_EXTRACT_PATH}' && tar -xf '{tmp_tar}'")
                        item_ssh.execute_command(f"rm -f '{tmp_tar}'")

                        if code != 0:
                            with self._print_lock:
                                log(f"  [ERROR] [{idx}] Tar extraction failed for {filename}: {err}")
                                self.failed_wars.append(war_prefix)
                                self.failed_routes[war_prefix] = route_idx
                            return False

                        with self._print_lock:
                            log(f"  [SUCCESS] [{idx}/{len(mappings_to_process)}] Uploaded & extracted {filename}")
                        return True

                except Exception as e:
                    with self._print_lock:
                        log(f"  [ERROR] [{idx}] Upload failed for {war_prefix} via {route['host']}: {e}")
                        self.failed_wars.append(war_prefix)
                        self.failed_routes[war_prefix] = route_idx
                    return False
                finally:
                    item_ssh.close()

            items = list(enumerate(mappings_to_process, 1))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(_upload_item, items))

            success_count = sum(1 for r in results if r)
            log("\n" + "=" * 60)
            log(f"[SUCCESS] Step 2 Complete! {success_count}/{len(mappings_to_process)} artifacts uploaded & staged.")
            log("=" * 60 + "\n")

            # Deploy WAR files to final folders
            log("[INFO] Deploying staged WAR files to production directories...")
            for war_prefix, deploy_folder in mappings_to_process:
                war_file = f"{war_prefix}-{self.config.VERSION}.war"
                source_war = f"{self.config.TARGET_EXTRACT_PATH}/{war_file}"
                target_dir = f"{self.config.TARGET_DEPLOY_BASE}/{deploy_folder}/{self.config.VERSION}/War"
                log(f"  [DEPLOY] {war_file} -> {deploy_folder}")
                ssh.execute_command(f"mkdir -p '{target_dir}' && cp '{source_war}' '{target_dir}/'")

            ssh.execute_command("rm -f /tmp/*.tar /tmp/*.war 2>/dev/null || true")
            return success_count == len(mappings_to_process)

        finally:
            ssh.close()

    def run_step3_deploy(
        self,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """Step 3: Deploy artifacts into final production application directories."""
        def log(msg: str):
            if log_callback:
                log_callback(msg)
            else:
                try:
                    print(msg)
                except UnicodeEncodeError:
                    print(msg.encode('ascii', errors='replace').decode('ascii'))

        log("\n" + "=" * 60)
        log("STEP 3: Deploy Artifacts to Application Folders")
        log("=" * 60 + "\n")

        routes = self.config.TARGET_ROUTES if self.config.TARGET_ROUTES else [{'host': self.config.TARGET_SERVER, 'username': self.config.TARGET_USER}]
        target_route = routes[0]

        ssh = SSHClient(
            hostname=target_route['host'],
            username=target_route['username'],
            password=self.config.TARGET_PASSWORD,
            port=self.config.TARGET_PORT
        )
        ssh.connect()

        try:
            deployed_count = 0
            for idx, (war_prefix, deploy_folder) in enumerate(self.config.WAR_MAPPINGS, 1):
                if self.check_cancelled():
                    log("[INFO] Deployment cancelled by user.")
                    break

                war_file = f"{war_prefix}-{self.config.VERSION}.war"
                source_war = f"{self.config.TARGET_EXTRACT_PATH}/{war_file}"
                target_dir = f"{self.config.TARGET_DEPLOY_BASE}/{deploy_folder}/{self.config.VERSION}/War"

                log(f"[{idx}/{len(self.config.WAR_MAPPINGS)}] Deploying: {war_file} -> {deploy_folder}...")
                cmd = f"mkdir -p '{target_dir}' && cp '{source_war}' '{target_dir}/'"
                out, err, code = ssh.execute_command(cmd)

                if code == 0:
                    log(f"  [SUCCESS] Successfully deployed to {target_dir}/")
                    deployed_count += 1
                else:
                    log(f"  [ERROR] Deployment failed for {war_file}: {err}")

            ssh.execute_command("rm -f /tmp/*.tar /tmp/*.war 2>/dev/null || true")

            log("\n" + "=" * 60)
            log(f"[SUCCESS] Step 3 Complete! {deployed_count}/{len(self.config.WAR_MAPPINGS)} components deployed to production.")
            log("=" * 60 + "\n")
            return deployed_count == len(self.config.WAR_MAPPINGS)

        finally:
            ssh.close()

    def run_all(
        self,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[str, str, int, int], None]] = None
    ) -> bool:
        """Run full 3-step deployment pipeline end-to-end."""
        downloaded = self.run_step1_download(log_callback, progress_callback)
        if self.check_cancelled() or not downloaded:
            return False

        uploaded = self.run_step2_upload(log_callback, progress_callback)
        if self.check_cancelled() or not uploaded:
            return False

        return self.run_step3_deploy(log_callback)
