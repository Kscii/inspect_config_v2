"""
OBS 客户端模块
封装 obsutil 命令行工具的调用
"""
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ..utils import logger


class ObsClient:
    """OBS 客户端类"""
    
    def __init__(self, config_path: str, obsutil_exe: str = 'obsutil'):
        """
        初始化 OBS 客户端
        
        Args:
            config_path: obsutil 配置文件路径
            obsutil_exe: obsutil 可执行文件路径
        """
        self.config_path = config_path
        self.obsutil_exe = obsutil_exe or 'obsutil'
        
        # 验证配置文件
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"obsutil 配置文件不存在: {config_path}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=10),
        retry=retry_if_exception_type(subprocess.TimeoutExpired),
        reraise=True
    )
    def test_connection(self, bucket: str) -> bool:
        """
        测试 OBS 连接是否正常
        
        Args:
            bucket: bucket 名称
            
        Returns:
            连接是否正常
        """
        logger.info(f"测试 OBS 连接: {bucket}")
        
        cmd = [
            self.obsutil_exe, 'ls', f"obs://{bucket}/",
            '-config', self.config_path,
            '-limit', '1'
        ]
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info(f"✓ OBS 连接正常: {bucket}")
                return True
            else:
                logger.error(f"✗ OBS 连接失败: {bucket}")
                logger.error(f"返回码: {result.returncode}")
                logger.error(f"STDERR: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.error(f"✗ OBS 连接超时: {bucket}")
            raise
        except Exception as e:
            logger.error(f"✗ OBS 连接异常: {e}")
            return False
    
    def list_files(self, bucket: str, path: str, limit: int = 1000, max_total: int = 50000) -> List[Dict]:
        """
        列出指定路径下的所有文件（支持分页）
        
        Args:
            bucket: bucket 名称
            path: 路径前缀
            limit: 每页最大文件数
            max_total: 最大总文件数限制
            
        Returns:
            文件信息列表，每项包含：
            {
                'path': 'obs://bucket/path',
                'last_modified': '2026-01-26T05:56:18Z'
            }
        """
        path = path.strip('/')
        obs_uri = f"obs://{bucket}/{path}/"
        
        logger.info(f"列出 OBS 文件: {obs_uri}")
        
        all_files = []
        marker = None
        page = 0
        
        while True:
            page += 1
            files, next_marker = self._list_files_page(bucket, path, obs_uri, marker, limit)
            all_files.extend(files)
            
            logger.info(f"第 {page} 页: 找到 {len(files)} 个文件，累计 {len(all_files)} 个")
            
            # 检查是否达到最大限制
            if len(all_files) >= max_total:
                logger.warning(f"已达到最大文件数限制 {max_total}，停止列举")
                break
            
            # 检查是否还有下一页
            if not next_marker:
                logger.info(f"已列出所有文件，共 {len(all_files)} 个")
                break
            
            marker = next_marker
        
        return all_files
    
    @retry(
        stop=stop_after_attempt(2),  # 从3次减少到2次
        wait=wait_exponential(multiplier=1, min=1, max=5),  # 减少等待时间
        retry=retry_if_exception_type((subprocess.CalledProcessError, subprocess.TimeoutExpired)),
        reraise=True
    )
    def _list_files_page(self, bucket: str, path: str, obs_uri: str, marker: Optional[str], limit: int) -> tuple[List[Dict], Optional[str]]:
        """
        列出一页文件
        
        Args:
            bucket: bucket 名称
            path: 路径前缀
            obs_uri: 完整 OBS URI
            marker: 分页标记
            limit: 每页最大文件数
            
        Returns:
            (文件信息列表, 下一页marker)
            文件信息包含: {'path': str, 'last_modified': str}
        """
        cmd = [
            self.obsutil_exe, 'ls', obs_uri,
            '-config', self.config_path,
            '-limit', str(limit),
            '-s',
        ]
        if marker:
            cmd.extend(['-marker', marker])
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5分钟超时
                check=False
            )
            
            if result.returncode != 0:
                logger.error(f"obsutil ls 失败 (返回码 {result.returncode})")
                logger.error(f"STDERR: {result.stderr}")
                return [], None
            
            # 解析输出
            # 格式示例：
            # obs://bucket/path/file.json    2026-01-26T05:56:18Z    134.13KB  standard  "etag"
            files = []
            next_marker = None
            
            for line in result.stdout.split('\n'):
                line = line.strip()
                
                # 空行跳过
                if not line:
                    continue
                
                # 检查是否是 marker 行
                if line.startswith('Next marker:'):
                    next_marker = line.replace('Next marker:', '').strip()
                    continue
                
                # 检查是否是统计行（Folder number / File number）
                if 'Folder number:' in line or 'File number:' in line:
                    continue
                
                # 解析文件信息：obs路径、时间戳、大小、存储类型、etag
                # 使用空格分割，但要注意路径可能包含空格
                parts = line.split()
                if len(parts) >= 2:
                    # 第一个部分是路径（可能是完整 obs:// 或相对路径）
                    file_path = parts[0]
                    # 第二个部分是时间戳（ISO 8601 格式）
                    last_modified = parts[1] if len(parts) > 1 else None
                    
                    # 补充完整 URI（如果是相对路径）
                    if not file_path.startswith('obs://'):
                        file_path = f"obs://{bucket}/{file_path}"
                    
                    if last_modified:
                        files.append({
                            'path': file_path,
                            'last_modified': last_modified
                        })
            
            return files, next_marker
            
        except subprocess.TimeoutExpired:
            logger.error("obsutil ls 超时")
            raise
        except Exception as e:
            logger.error(f"obsutil ls 异常: {e}")
            return [], None
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=10),
        retry=retry_if_exception_type((subprocess.CalledProcessError, subprocess.TimeoutExpired)),
        reraise=True
    )
    def download_file(self, obs_path: str, local_path: Path) -> bool:
        """
        下载文件到本地
        
        Args:
            obs_path: OBS 文件路径
            local_path: 本地文件路径
            
        Returns:
            是否下载成功
        """
        # 如果文件已存在，跳过下载
        if local_path.exists():
            logger.debug(f"文件已存在，跳过下载: {local_path.name}")
            return True
        
        # 确保目录存在
        local_path.parent.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            self.obsutil_exe, 'cp',
            obs_path,
            str(local_path),
            '-f',  # 强制覆盖
            '-config', self.config_path
        ]
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,  # 2分钟超时
                check=True
            )
            logger.debug(f"下载成功: {local_path.name}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"下载失败 {obs_path}: {e.stderr}")
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"下载超时 {obs_path}")
            raise
