# 仓根 conftest:让 pytest 把仓根加进 sys.path(import nursery 可解析),
# 并兜住任何测试误用真实存档目录——未显式指 NURSERY_SAVES_DIR 的用例
# 会落到这个不可写路径直接炸出来,而不是默默写 nursery/saves/。
import os

os.environ.setdefault("NURSERY_SAVES_DIR", "/nonexistent/nursery-test-guard")
