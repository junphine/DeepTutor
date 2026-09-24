"""Small authenticated HTTP/SSE client for QwenPaw Agent gateway."""
import os
import base64
from pathlib import Path
from typing import List, Dict, Tuple
import requests
import json
import uuid
from datetime import datetime

def is_image_by_ext(filepath):
    image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg', '.ico'}
    ext = os.path.splitext(filepath)[1].lower()
    return ext in image_exts

class QwenPawRemoteClient:
    """通用Http连接器"""

    def __init__(
            self,
            base_url: str = "",
            agent_id: str = "default",
            skill_name: str = "",
            username: str = None,
            password: str = None,
            file_extensions: list = []
    ):
        """
        初始化生成器

        Args:
            base_url: QwenPaw 服务地址
            agent_id: Agent ID
            skill_name: 要调用的技能名称
            username: 用户名（如果需要认证）
            password: 密码（如果需要认证）
            file_extensions: 要处理的文件扩展名列表（如 ['.md', '.txt', '.png']），None 表示处理所有文件
            only_path: 是否只发送文件路径给agent的skill（如果skill支持文件路径作为输入）
        """
        self.base_url = base_url or "http://localhost:8088"
        self.api_url = f"{self.base_url}/api/console/chat"
        self.login_url = f"{self.base_url}/api/auth/login"
        self.agent_id = agent_id
        self.skill_name = skill_name
        self.username = username
        self.password = password
        self.auth_token = None
        self.file_extensions = file_extensions or ["md", "json", "txt", "csv", "xml", "html", "yml","jpg","png","svg","gif","webp"]

        # 自动登录
        if username and password:
            self._login()

    def _login(self) -> bool:
        """登录获取认证令牌"""
        try:
            response = requests.post(
                self.login_url,
                json={"username": self.username, "password": self.password},
                timeout=30
            )

            if response.status_code == 200:
                self.auth_token = response.json().get("token")
                print("+ 登录成功")
                return True
            else:
                print(f"x 登录失败：HTTP {response.status_code}")
                return False

        except Exception as e:
            print(f"x 登录异常：{e}")
            return False

    def version(self):
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Id": self.agent_id
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        with requests.get(
                f"{self.base_url}/api/version",
                headers=headers,
                timeout=6
        ) as response:
            if response.status_code != 200:
                return response.status_code, response.text
            data = json.loads(response.text)
            return 200, data['version']

    def scan_files(self, directory: str, recursive: bool = True) -> List[Path]:
        """
        扫描指定目录下的所有文件

        Args:
            directory: 要扫描的目录路径
            recursive: 是否递归扫描子目录

        Returns:
            List[Path]: 文件路径列表
        """
        dir_path = Path(directory)

        if not dir_path.exists():
            raise FileNotFoundError(f"目录不存在：{directory}")

        if not dir_path.is_dir():
            return [dir_path]
            #raise NotADirectoryError(f"不是目录：{directory}")

        # 如果指定了扩展名，则只扫描那些扩展名的文件
        if self.file_extensions:
            files = []
            for ext in self.file_extensions:
                pattern = f"**/*{ext}" if recursive else f"*{ext}"
                files.extend(dir_path.glob(pattern))
        else:
            # 否则扫描所有文件
            pattern = "**/*" if recursive else "*"
            files = list(dir_path.glob(pattern))

        # 过滤掉以 . 开头的隐藏文件和目录
        files = [f for f in files if f.is_file() and not f.name.startswith('.')]

        return sorted(files)

    def read_file(self, file_path: Path, max_length: int = 50000) -> str:
        """
        读取文件内容

        Args:
            file_path: 文件路径
            max_length: 最大读取字符数（避免超出 token 限制）

        Returns:
            str: 文件内容
        """
        try:
            # 尝试 UTF-8 编码
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 如果内容过长，截取前 max_length 个字符
            if len(content) > max_length:
                print(f"  ! 文件内容过长 ({len(content)} 字符)，截取前 {max_length} 字符")
                content = content[:max_length] + "\n\n[内容过长，已截断...]"

            return content

        except UnicodeDecodeError:
            # 尝试其他编码
            try:
                with open(file_path, 'r', encoding='gbk') as f:
                    content = f.read()
                if len(content) > max_length:
                    content = content[:max_length] + "\n\n[内容过长，已截断...]"
                return content
            except Exception as e:
                print(f"  x 读取文件失败（编码问题）: {e}")
                return ""
        except Exception as e:
            print(f"  x 读取文件失败：{e}")
            return ""

    def prompt(
            self,
            content: List[Dict],
            save_to_path: str | None = None,
            session_id: str = None
    ) -> Dict:
        """
        基于内容生成输出

        Args:
            content: 文件内容
            save_to_path: 保存路径
        Returns:
            Dict: 包含生成结果
        """
        # 构建请请求
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Id": self.agent_id
        }

        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        data = {
            "input": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "session_id": session_id,
            "user_id": self.username or "deeptutor",
            "channel": "console",
            "request_context": {
                "session_project_dir": save_to_path
            }
        }

        result = {
            "content": "",
            "success": False,
            "error": None
        }

        try:
            with requests.post(
                    self.api_url,
                    headers=headers,
                    json=data,
                    stream=True,
                    timeout=600
            ) as response:
                if response.status_code != 200:
                    result["error"] = f"HTTP {response.status_code}: {response.text}"
                    return result

                for line in response.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith('data: '):
                            event_data = json.loads(line[6:])

                            # 检查错误
                            if event_data.get('error'):
                                error_msg = event_data['error'].get('message', '未知错误')
                                result["error"] = error_msg
                                print(f"\n x 错误：{error_msg}")
                                break

                            # 处理输出
                            if event_data.get('output'):
                                for item in event_data['output']:
                                    if item.get('role') == 'assistant':
                                        for content_item in item.get('content', []):
                                            if content_item.get('type') == 'text':
                                                text = content_item.get('text', '')
                                                result["content"] += text
                                                print(text, end='', flush=True)

            result["success"] = result["error"] is None

        except requests.exceptions.Timeout:
            result["error"] = "请求超时（600 秒）"
            print(f"\n x 请求超时")
        except Exception as e:
            result["error"] = str(e)
            print(f"\n x 请求异常：{e}")

        return result

    def process_directory(
            self,
            prompt: List[Dict],
            directory: List[str]|str,
            output_dir: str = "",
            recursive: bool = True,
            only_path: bool = False,
            session_id: str = ""
    ) -> Dict:
        """
        处理整个目录的文件

        Args:
            prompt: 用户提示词
            directory: 输入目录
            output_dir: 输出目录
            recursive: 是否递归扫描子目录
            only_path: 只发送路径

        Returns:
            Dict: 处理结果统计
        """

        # 扫描文件
        try:
            files = []
            if type(directory)==type([]):
                for item in directory:
                    files += self.scan_files(item, recursive)
            else:
                files = self.scan_files(directory, recursive)
        except Exception as e:
            print(f"x 扫描失败：{e}")
            return {"success": False, "error": str(e)}

        if not files:
            print("x 未找到任何文件")
            return {"success": False, "error": "未找到文件"}

        print(f"找到 {len(files)} 个文件:\n")
        for i, f in enumerate(files, 1):
            print(f"  {i}. {str(f)}")
        print()

        # 创建输出目录
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

        root = Path(directory)
        result = None
        # 处理统计
        stats = {
            "total_files": len(files),
            "success_count": 0,
            "failed_count": 0,
            "files": []
        }

        # 为每个文件单独生成输出
        print("\n" + "=" * 70)
        print("模式：为每个文件单独生成输出")
        print("=" * 70)

        for i, file_path in enumerate(files, 1):
            print(f"\n[{i}/{len(files)}] 处理：{file_path.name}")
            print("-" * 70)
            absrel = file_path.relative_to(root)
            is_image = is_image_by_ext(absrel)
            if only_path:
                content = f"附件路径: {file_path}"
                prompt.append({"type":"text","text":content})
            else:
                # 读取文件
                content = self.read_file(file_path)
                if not content:
                    print(f"  x 跳过（无法读取）")
                    stats["failed_count"] += 1
                    stats["files"].append({
                        "file": file_path.name,
                        "error": "无法读取文件",
                        "success": False
                    })
                    continue
                print(f"  读取成功 ({len(content)} 字符)")
                if not is_image:
                    prompt.append({"type":"text","text":content})
                else:
                    base64_image = base64.b64encode(content).decode("utf-8")
                    prompt.append({"type":"image","image_url":f"data:image/jpeg;base64,{base64_image}"})
            print(f"  开始生成输出...")

            # 生成输出
            result = self.prompt(
                content=prompt,
                save_to_path=str(absrel.parent),
                session_id=session_id
            )
            if result["success"]:
                stats["success_count"] += 1
                # 保存结果
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_name = absrel.replace(' ', '_').replace('-', '_')
                if output_dir:
                    output_file = Path(output_dir) / f"output_{safe_name}_{timestamp}.md"

                    # 添加头部信息
                    header = f"""# 输入文件: {file_path.name}\n"""

                    with open(output_file, 'w', encoding='utf-8') as f:
                        f.write(header + result["content"])

                    print(f"\n  输出已保存：{output_file}")
                    stats["files"].append({
                        "file": file_path.name,
                        "output_file": str(output_file),
                        "success": True
                    })
                else:
                    stats["files"].append({
                        "file": file_path.name,
                        "output_content": result["content"],
                        "success": True
                    })
            else:
                stats["failed_count"] += 1
                stats["files"].append({
                    "file": file_path.name,
                    "error": result["error"],
                    "success": False
                })

        if len(files)==1:
            return result

        # 打印统计
        print("\n" + "=" * 70)
        print("处理完成统计")
        print("=" * 70)
        print(f"总文件数：{stats['total_files']}")
        print(f"成功：{stats['success_count']}")
        print(f"失败：{stats['failed_count']}")
        print(f"输出目录：{output_dir}")
        print("=" * 70)
        result['content'] = stats
        return result
