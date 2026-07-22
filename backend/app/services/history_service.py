"""画布项目和聊天记录的本地 JSON 持久化服务。

当前项目没有使用数据库，而是把所有 Canvas 对象保存在
``backend/storage/chat_history.json`` 的一个 JSON 数组中。服务每次修改时会：
读取整个数组 -> 在内存中修改 -> 将整个数组重新写入文件。

这种实现简单且适合本地单用户项目；如果以后需要多进程、高并发或大量项目，
则应改用带并发控制的数据库或至少增加文件锁和原子写入。
"""

import json
import os
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 这是相对路径，通常以 backend 作为进程工作目录启动，因此最终指向
# backend/storage/chat_history.json。
HISTORY_FILE = "storage/chat_history.json"


class Canvas(BaseModel):
    """一个画布项目的预期数据结构。

    当前 ``POST /api/canvases`` 为了完整保留 Excalidraw 的扩展字段，会直接保存
    原始字典而不实例化该模型；这个模型仍用于描述项目的核心字段和类型。
    """

    # 画布的唯一标识，也是 URL 中 canvasId 和 WebSocket 分组使用的值。
    id: str
    # 项目在首页和侧边栏中显示的名称。
    name: str
    # 创建时间，前后端约定为毫秒时间戳。
    createdAt: float
    # 兼容旧版 DOM 拖拽画布保存的图片列表，新数据通常使用下面的 data 字段。
    images: Optional[List[Dict[str, Any]]] = []
    # Excalidraw 画布数据，通常包含 elements、appState 和 files。
    data: Optional[Dict[str, Any]] = None
    # 当前画布关联的用户消息、助手回复和工具结果。
    messages: List[Dict[str, Any]]


class HistoryService:
    """负责画布列表的初始化、读取、保存和删除。"""

    def __init__(self):
        self.file_path = HISTORY_FILE
        # 服务对象创建时立即准备目录和 JSON 文件，后续接口可直接读写。
        self._ensure_storage_dir()

    def _ensure_storage_dir(self):
        """确保父目录和一个合法的初始 JSON 数组存在。"""

        # dirname("storage/chat_history.json") 得到 "storage"。
        # exist_ok=True 表示目录已经存在时不会抛出异常。
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        if not os.path.exists(self.file_path):
            # 第一次启动时创建内容为 [] 的历史文件。
            self._save_data([])
        else:
            # 文件已经存在时，先检查内容是否为空或不是合法 JSON。
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if not content:
                        logger.warning("历史记录文件为空，重置为空列表")
                        self._save_data([])
                    else:
                        # 此处只验证格式，不需要保存 json.loads() 的解析结果。
                        json.loads(content)
            except (json.JSONDecodeError, Exception) as e:
                # 初始化阶段发现异常时，重置为空数组，使应用仍能正常启动。
                logger.warning(f"初始化时发现历史记录文件格式错误: {e}，重置为空列表")
                self._save_data([])

    def _load_data(self) -> List[Dict[str, Any]]:
        """读取并解析整个历史文件，失败时返回空列表。"""

        try:
            # 文件可能在程序运行期间被人为删除，因此读取前仍需检查。
            if not os.path.exists(self.file_path):
                return []
            with open(self.file_path, 'r', encoding='utf-8') as f:
                # strip() 用于区分只包含空格或换行的空文件。
                content = f.read().strip()
                if not content:
                    logger.warning(f"历史记录文件为空，返回空列表")
                    return []
                # json.loads() 把 JSON 数组转换成 Python 的 list[dict]。
                return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"历史记录文件格式错误: {e}，尝试修复...")
            # 文件内容不是合法 JSON 时，尽量先备份原文件，再恢复成空数组。
            try:
                backup_path = self.file_path + '.backup'
                if os.path.exists(self.file_path):
                    import shutil
                    # copy2 除了文件内容，还尽量保留原文件的时间等元数据。
                    shutil.copy2(self.file_path, backup_path)
                    logger.info(f"已备份损坏文件到: {backup_path}")
                self._save_data([])
                return []
            except Exception as backup_error:
                # 连备份或重置也失败时，仍返回空列表，避免读取接口直接崩溃。
                logger.error(f"备份文件失败: {backup_error}")
                return []
        except Exception as e:
            # 权限、磁盘或其他 I/O 异常统一记录，调用方得到可处理的空列表。
            logger.error(f"加载历史记录失败: {e}")
            return []

    def _save_data(self, data: List[Dict[str, Any]]):
        """将完整的画布列表覆盖写入历史文件。"""

        try:
            # "w" 会先清空旧文件，再写入新的完整 JSON 内容。
            with open(self.file_path, 'w', encoding='utf-8') as f:
                # ensure_ascii=False 保留中文；indent=2 让文件便于人工查看和调试。
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            # 当前设计只记录保存失败，不继续向上抛出异常。
            logger.error(f"保存历史记录失败: {e}")

    def get_canvases(self) -> List[Dict[str, Any]]:
        """返回历史文件中的全部画布项目。"""
        return self._load_data()

    def save_canvas(self, canvas_data: Dict[str, Any]):
        """按 id 更新已有画布；id 不存在时把新画布插入列表开头。"""

        canvases = self._load_data()

        # -1 表示尚未找到；enumerate 同时给出索引 i 和画布字典 c。
        index = -1
        for i, c in enumerate(canvases):
            # dict.get() 在字段缺失时返回 None，避免直接使用 [] 产生 KeyError。
            if c.get('id') == canvas_data.get('id'):
                index = i
                break
        
        if index >= 0:
            # 找到相同 id 时整体替换，画布数据和消息都会以传入内容为准。
            canvases[index] = canvas_data
        else:
            # insert(0, ...) 让最新创建的项目出现在列表最前面。
            canvases.insert(0, canvas_data)
            
        self._save_data(canvases)
        # 返回传入对象，路由层可以直接把它作为 JSON 响应交给前端。
        return canvas_data

    def delete_canvas(self, canvas_id: str):
        """删除所有 id 等于 canvas_id 的画布并覆盖保存。"""

        canvases = self._load_data()
        # 列表推导式只保留 id 不匹配的项目，相当于过滤掉待删除画布。
        canvases = [c for c in canvases if c.get('id') != canvas_id]
        self._save_data(canvases)
        # 当前接口无论是否找到目标都返回 True，表示删除流程已执行完毕。
        return True


# 模块级单例：其他模块导入 history_service 后共享同一个服务对象。
# 构造函数会在首次导入本模块时运行，从而提前初始化 storage 和历史文件。
history_service = HistoryService()






