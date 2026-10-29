"""跨项目保持一致的标签与文本约定。"""
SOURCE_LABEL_DEFAULT = 'source::agent'
REVIEW_LABELS = frozenset({
    'review::待评审', 'review::需修改', 'review::已确认', 'review::可合入',
    'scope::确认修复', 'scope::暂不修复', 'priority::高', 'priority::中', 'priority::低',
})
ALL_LABELS = REVIEW_LABELS | {'等待前置', '推断未复现'}
TEXT_ONLY_MARK = '修改类型：仅文字'
ACTION_MARKERS = ('hermes 需修改正文', 'hermes 需执行', '对修复方案的意见')
USER_MENTION_KEY = 'accounts.mention'
USER_MENTION_TEMPLATE = '@{username}'


# 同意评论不超过 200 字；正则与线上保持一致。
import re
APPROVAL_RE = re.compile(r"^\s*(lgtm|同意合入|同意合并|可以合入|approved?)(\W|$)", re.I)
REVOKE_RE = re.compile(r"^\s*(暂缓合入|先不合入|不要合入|先别合|hold)(\W|$)", re.I)
