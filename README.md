# PHP 5.3.29：实验性 FPM 安全回补

**不是官方安全版本，不宣称完整修复两个 CVE，也不要直接替换生产镜像。**
本仓库保留完整 PHP 5.3.29 源码、原许可证和可审查的独立修改记录。

## 本次处理范围

| 风险 | 本仓库措施 | 限制 |
|---|---|---|
| CVE-2026-109618：OOM longjmp 后清理未完成状态 | Zend 默认分配器的内存不足、分配大小溢出路径直接 `_exit(1)`，不再调用 PHP 错误回调或请求清理 | 不等同于受支持 PHP 分支的完整软 OOM 补丁；未覆盖第三方分配器、所有扩展自有分配失败路径 |
| CVE-2026-109619：后续请求使用悬空全局状态 | FPM 源码强制每个 child 只处理一个请求，完成 FastCGI 响应后 `_exit(0)` | 这是阻断跨请求复用，不是全面重置全局状态的补丁；其他 SAPI 不受此措施保护 |

PHP 5.3 没有 PHP 5.4 引入的 session upload progress，也没有 PHP 8.5 的
fatal_error_backtraces。缺少已公开的消费者不证明不存在其他漏洞链。

设计参考 PHP 安全团队的 PHP 7.4 最小回补，见 [UPSTREAM.md](UPSTREAM.md)。
与现代 PHP 的完整修复不同，这里选择旧分支可审查的 fail-stop 防护。

### 行为变化

- OOM 立即终止当前工作进程。不会执行 shutdown function、析构、缓冲输出或 session 保存。
  前端可能收到 502、空响应或已发送的部分响应。数据库事务和外部副作用必须由业务自行验证。
- 即使 FPM 配置 `pm.max_requests=0`，源码也强制单请求工作进程。FPM master 负责补充 child。
  性能会下降，Redis/MySQL 持久连接不能跨请求复用。正常请求的 PHP 清理仍执行。
- 本构建只支持 Linux x86_64、非线程 FPM、默认 Zend 分配器。CLI 仅用于检查。
  不支持 mod_php、CGI、ZTS、APC、自定义分配器或尚未审计的第三方扩展。
- 仍需请求大小限制、容器内存限制、网络隔离和现有业务安全加固。

## GitHub Actions：自动编译与隔离回归

Push、PR 和手动触发运行三组：原始 baseline、patched、AddressSanitizer patched。
baseline **故意保留原始行为**，只作为隔离对照，不可部署。
工作流权限只读，不读取生产凭据，不访问生产系统，不自动发布或部署。

检查包括：

1. PHP memory_limit OOM：对照组执行 shutdown 标记，修补组直接退出。
2. 非 ASan 组通过仅 CI 使用的 LD_PRELOAD 注入 mmap 分配失败，检查相同退出策略。
3. FPM 单 child，配置无限复用，并主动发送 KEEP_CONN：对照组复用 PID，修补组 PID 不重复。
4. fatal error 后的早期 INI 诊断、OOM 后 master 恢复、正常上传、超限 POST。
5. mysql、mysqli、PDO MySQL、mbstring、session、filter 的加载及基础 session 行为。

这不是可利用 RCE 的复现，也不是覆盖所有状态的模糊测试或完整安全认证。
ASan 开启不代表一定能观察 Zend 内部所有对象的越界或释放后使用。
检查日志、`checks.json`、PID 证据、编译产物及 SHA256SUMS 保留七天。

Ubuntu 22.04 手工执行（需 build-essential、pkg-config、Python 3、Git）：

```sh
bash ci/build.sh patched
# 单独目录，也可运行 baseline / asan。重跑前使用一个干净的 checkout。
```

产物在 `artifacts/patched/php/`。当前是验证构建，**不是现有业务镜像的直接替代品**：
没有编译 curl、Redis 扩展，也没有验证真实数据库、登录、加解密或所有业务页面。
二进制针对 Ubuntu 22.04，不能复制到 CentOS 7 或不同 glibc 镜像直接运行。
流水线通过后，下一步才是在目标容器基础系统编译所需扩展和进行业务验收。

PHP 5.3 已停止维护多年。本项目只降低所述两类风险，不修复其余历史漏洞。
最终应迁移至受支持的运行时，或完成 Go 重写。
