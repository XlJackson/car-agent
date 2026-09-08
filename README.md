# Car Agent：s04 Hooks

用最小 Python 代码学习 Agent Loop、工具分发、权限检查和 Hooks，后续接入车书、车旅和车辆状态工具。

## 启动与验证

在项目目录中，配置本地 `.env` 的 `ANTHROPIC_API_KEY`、`MODEL_ID`，以及按需设置的 `ANTHROPIC_BASE_URL`。不要提交密钥。

```bash
python src/car_agent/agent_loop.py
# 安装项目后也可以使用：
car-agent

# 离线测试，不调用真实模型
python -m unittest discover -s tests -v
```

工作目录是启动程序时的目录。每个用户任务最多调用模型 15 轮，可在 `config.py` 修改 `MAX_ROUNDS`；一轮可以有多个工具调用。达到上限可能意味着任务未完成，上限不限制单轮耗时。`q` / `exit` 在等待新问题时生效，运行中可按 Ctrl+C 退出（审批输入处按 Ctrl+C 表示拒绝本次操作）。

## 文件职责

```text
src/car_agent/
├── agent_loop.py   # 主循环、统一执行入口、CLI
├── config.py       # 环境变量、工作目录、提示词、轮数上限
├── tools.py        # 工具 Schema、实现和 TOOL_HANDLERS
├── permissions.py  # 硬拒绝、规则匹配、用户审批
├── hooks.py        # HookRegistry、回调、默认注册
└── __init__.py     # 包命令入口
```

按职责拆分，不按章节复制多个运行版本；章节版本由 Git 保存。工具变多后再将 `tools.py` 拆成 `tools/` 包，按车书、车旅、车辆等领域组织。

## Hook 的控制约定

| 事件 | 回调参数 | 返回值含义 |
|---|---|---|
| UserPromptSubmit | `query, history` | 忽略返回值；可原地向 history 加入上下文 |
| PreToolUse | `tool_name, args` | `None` 放行；其他值阻止工具，作为错误结果回传 |
| PostToolUse | `tool_name, args, result` | 忽略返回值；工具执行成功或抛异常后触发，拒绝时不触发 |
| Stop | `task_messages` | `None` 正常结束；其他值作为续跑指令回填模型 |

回调按注册顺序执行；PreToolUse 和 Stop 在首个非 None 返回值处短路。执行前回调异常会阻止工具；其他事件回调异常打印简短提示并继续。硬轮数上限不触发可续跑的 Stop，不能被 Hook 绕过。

默认注册：权限检查 → PreToolUse、大输出提示 → PostToolUse、本次请求统计 → Stop。UserPromptSubmit 暂无默认回调。普通终端日志只打印工具数量和名称，审批时展示目标路径或完整命令。

例如，在 `hooks.py` 添加以下函数，再在 `create_default_hooks()` 返回前注册，就能观察执行后事件，不必改主循环：

```python
def short_result_hook(tool_name, args, result):
    status = "失败" if result["is_error"] else "已返回"
    print(f"[Hook] {tool_name}：{status}")

# 放在 create_default_hooks() 的 return registry 之前：
registry.register_hook("PostToolUse", short_result_hook)
```

这里的状态来自工具结果。现有 bash handler 仍把命令输出或超时信息作为文本返回，`is_error` 并不完整代表 Shell 退出码。

## 扩展边界

新增工具：实现函数、添加 Schema、注册 TOOL_HANDLERS。新增执行前后行为：定义 Hook 回调、注册对应事件。Hooks 由程序确定时机执行，不是模型选择调用的工具。

s03 的权限规则原样迁移：文件工具访问工作区外需批准，glob 仅工作区内；bash 仍是教学用字符串和正则检查，不能完整限制越界访问。Hooks 提供扩展位置，本身不增强命令隔离。没有启用自动 git add、commit 或消息发送。
