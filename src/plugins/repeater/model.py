from typing import Generator, List, Optional, Union, Tuple, Dict, Any
from functools import cached_property, cmp_to_key
from dataclasses import dataclass
from collections import defaultdict

import jieba_fast.analyse
import threading
import pypinyin
import pymongo
import time
import random
import re
import atexit

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import Message, MessageSegment

mongo_client = pymongo.MongoClient('127.0.0.1', 27017, w=0)

mongo_db = mongo_client['PallasBot']

message_mongo = mongo_db['message']
message_mongo.create_index(name='time_index',
                           keys=[('time', pymongo.DESCENDING)])

context_mongo = mongo_db['context']
context_mongo.create_index(name='keywords_index',
                           keys=[('keywords', pymongo.HASHED)])
context_mongo.create_index(name='count_index',
                           keys=[('count', pymongo.DESCENDING)])
context_mongo.create_index(name='time_index',
                           keys=[('time', pymongo.DESCENDING)])
context_mongo.create_index(name='answers_index',
                           keys=[('answers.group_id', pymongo.TEXT),
                                 ('answers.keywords', pymongo.TEXT)],
                           default_language='none')

blacklist_mongo = mongo_db['blacklist']
blacklist_mongo.create_index(name='group_index',
                             keys=[('group_id', pymongo.HASHED)])


@dataclass
class ChatData:
    group_id: int
    user_id: int
    raw_message: str
    plain_text: str
    time: int

    _keywords_size: int = 3

    @cached_property
    def is_plain_text(self) -> bool:
        return '[CQ:' not in self.raw_message and len(self.plain_text) != 0

    @cached_property
    def is_image(self) -> bool:
        return '[CQ:image,' in self.raw_message or '[CQ:face,' in self.raw_message

    @cached_property
    def keywords(self) -> str:
        if not self.is_plain_text and len(self.plain_text) == 0:
            return self.raw_message

        keywords_list = jieba_fast.analyse.extract_tags(
            self.plain_text, topK=ChatData._keywords_size)
        if len(keywords_list) < 2:
            return self.plain_text
        else:
            # keywords_list.sort()
            return ' '.join(keywords_list)

    @cached_property
    def keywords_pinyin(self) -> str:
        return ''.join([item[0] for item in pypinyin.pinyin(
            self.keywords, style=pypinyin.NORMAL, errors='default')]).lower()

    @cached_property
    def to_me(self) -> bool:
        return '牛牛' in self.keywords or '帕拉斯' in self.keywords


class Chat:
    answer_threshold = 3            # answer 相关的阈值，值越小牛牛废话越多，越大话越少
    # answer_limit_threshold = 50     # 上限阈值，一般正常的上下文不可能发 50 遍，一般是其他 bot 的回复，禁了！
    cross_group_threshold = 2       # N 个群有相同的回复，就跨群作为全局回复
    repeat_threshold = 3            # 复读的阈值，群里连续多少次有相同的发言，就复读
    speak_threshold = 5             # 主动发言的阈值，越小废话越多

    drunk_probability = 0.07        # 牛牛喝醉的概率（回复没达到阈值的话）
    split_probability = 0.5         # 按逗号分割回复语的概率
    voice_probability = 0           # 回复语音的概率（仅纯文字）
    speak_continuously_probability = 0.5  # 连续主动说话的概率
    speak_continuously_max_len = 2  # 连续主动发言最多几句话

    save_time_threshold = 3600      # 每隔多久进行一次持久化 ( 秒 )
    save_count_threshold = 1000     # 单个群超过多少条聊天记录就进行一次持久化。与时间是或的关系

    blacklist_answer = defaultdict(set)
    blacklist_answer_reserve = defaultdict(set)

    def __init__(self, data: Union[ChatData, GroupMessageEvent, PrivateMessageEvent]):

        if (isinstance(data, ChatData)):
            self.chat_data = data
        elif (isinstance(data, GroupMessageEvent)):
            self.chat_data = ChatData(
                group_id=data.group_id,
                user_id=data.user_id,
                # 删除图片子类型字段，同一张图子类型经常不一样，影响判断
                raw_message=re.sub(
                    r',subType=\d+\]',
                    r']',
                    data.raw_message),
                plain_text=data.get_plaintext(),
                time=data.time
            )
        elif (isinstance(data, PrivateMessageEvent)):
            event_dict = data.dict()
            self.chat_data = ChatData(
                group_id=data.user_id,  # 故意加个符号，和群号区分开来
                user_id=data.user_id,
                # 删除图片子类型字段，同一张图子类型经常不一样，影响判断
                raw_message=re.sub(
                    r',subType=\d+\]',
                    r']',
                    data.raw_message),
                plain_text=data.get_plaintext(),
                time=data.time
            )

    def learn(self) -> bool:
        '''
        学习这句话
        '''

        if len(self.chat_data.raw_message.strip()) == 0:
            return False

        group_id = self.chat_data.group_id
        if group_id in Chat._message_dict:
            group_msgs = Chat._message_dict[group_id]
            if group_msgs:
                group_pre_msg = group_msgs[-1]
            else:
                group_pre_msg = None

            # 群里的上一条发言
            self._context_insert(group_pre_msg)

            user_id = self.chat_data.user_id
            if group_pre_msg and group_pre_msg['user_id'] != user_id:
                # 该用户在群里的上一条发言（倒序三句之内）
                for msg in group_msgs[:-3:-1]:
                    if msg['user_id'] == user_id:
                        self._context_insert(msg)
                        break

        self._message_insert()
        return True

    def answer(self, with_limit: bool = True) -> Optional[Generator[Message, None, None]]:
        '''
        回复这句话，可能会分多次回复，也可能不回复
        '''

        if with_limit:
            # 不回复太短的对话，大部分是“？”、“草”
            if self.chat_data.is_plain_text and len(self.chat_data.plain_text) < 2:
                return None

            if self.chat_data.group_id in Chat._reply_dict:
                group_replies = Chat._reply_dict[self.chat_data.group_id]
                if len(group_replies):
                    latest_reply = group_replies[-1]
                    # 限制发音频率，最多 6 秒一次
                    if int(time.time()) - latest_reply['time'] < 6:
                        return None
                    # # 不要一直回复同一个内容
                    # if self.chat_data.raw_message == latest_reply['pre_raw_message']:
                    #     return None
                    # 有人复读了牛牛的回复，不继续回复
                    # if self.chat_data.raw_message == latest_reply['reply']:
                    #    return None

                    # 如果连续 5 次回复同样的内容，就不再回复。这种情况很可能是和别的 bot 死循环了
                    # repeat_times = 5
                    # if len(group_replies) >= repeat_times \
                    #     and all(reply['pre_raw_message'] == self.chat_data.raw_message
                    #             for reply in group_replies[-repeat_times:]):
                    #     return None

        results = self._context_find()

        if results:
            with Chat._reply_lock:
                Chat._reply_dict[self.chat_data.group_id].append({
                    'time': int(time.time()),
                    'pre_raw_message': self.chat_data.raw_message,
                    'pre_keywords': self.chat_data.keywords,
                    'reply': '[PallasBot: Reply]',  # flag
                    'reply_keywords': '[PallasBot: Reply]',  # flag
                })

            def yield_results(results: Tuple[List[str], str]) -> Generator[Message, None, None]:
                group_replies = Chat._reply_dict[self.chat_data.group_id]
                answer_list, answer_keywords = results
                for item in answer_list:
                    with Chat._reply_lock:
                        group_replies.append({
                            'time': int(time.time()),
                            'pre_raw_message': self.chat_data.raw_message,
                            'pre_keywords': self.chat_data.keywords,
                            'reply': item,
                            'reply_keywords': answer_keywords,
                        })
                    if '[CQ:' not in item and len(item) > 1 \
                            and random.random() < Chat.voice_probability:
                        yield Chat._text_to_speech(item)
                    else:
                        yield Message(item)

                with Chat._reply_lock:
                    group_replies = group_replies[-Chat._save_reserve_size:]

            return yield_results(results)

        return None

    @staticmethod
    def speak() -> Optional[Tuple[int, List[Message]]]:
        '''
        主动发言，返回当前最希望发言的群号、发言消息 List，也有可能不发言
        '''

        basic_msgs_len = 10
        basic_delay = 600

        def group_popularity_cmp(lhs: Tuple[int, List[Dict[str, Any]]],
                                 rhs: Tuple[int, List[Dict[str, Any]]]) -> int:

            def cmp(a: Any, b: Any):
                return (a > b) - (a < b)

            lhs_group_id, lhs_msgs = lhs
            rhs_group_id, rhs_msgs = rhs

            lhs_len = len(lhs_msgs)
            rhs_len = len(rhs_msgs)

            # 默认是 0, 加个 1 避免乘没了
            lhs_drunkenness = Chat._drunkenness_dict[lhs_group_id] + 1
            rhs_drunkenness = Chat._drunkenness_dict[rhs_group_id] + 1

            if lhs_len < basic_msgs_len or rhs_len < basic_msgs_len:
                return cmp(lhs_len * lhs_drunkenness,
                           rhs_len * rhs_drunkenness)

            lhs_duration = lhs_msgs[-1]['time'] - lhs_msgs[0]['time']
            rhs_duration = rhs_msgs[-1]['time'] - rhs_msgs[0]['time']

            if not lhs_duration or not rhs_duration:
                return cmp(lhs_len, rhs_len)

            return cmp(lhs_len * lhs_drunkenness / lhs_duration,
                       rhs_len * rhs_drunkenness / rhs_duration)

        # 按群聊热度排序
        popularity = sorted(Chat._message_dict.items(),
                            key=cmp_to_key(group_popularity_cmp))

        cur_time = time.time()
        for group_id, group_msgs in popularity:
            if group_id not in Chat._reply_dict or len(group_msgs) < basic_msgs_len:
                continue
            if Chat._reply_dict[group_id][-1]['time'] > group_msgs[-1]['time']:
                continue

            msgs_len = len(group_msgs)
            latest_time = group_msgs[-1]['time']
            duration = latest_time - group_msgs[0]['time']
            avg_interval = duration / msgs_len

            # 已经超过平均发言间隔 N 倍的时间没有人说话了，才主动发言
            if cur_time - latest_time < avg_interval * Chat.speak_threshold + basic_delay:
                continue

            # append 一个 flag, 防止这个群热度特别高，但压根就没有可用的 context 时，每次 speak 都查这个群，浪费时间
            with Chat._reply_lock:
                Chat._reply_dict[group_id].append({
                    'time': int(cur_time),
                    'pre_raw_message': '[PallasBot: Speak]',
                    'pre_keywords': '[PallasBot: Speak]',
                    'reply': '[PallasBot: Speak]',
                    'reply_keywords': '[PallasBot: Speak]'
                })

            speak_context = context_mongo.aggregate([
                {
                    '$match': {
                        'time': {
                            '$gt': cur_time - 24 * 3600
                        },
                        'answers.count': {
                            '$gt': Chat.answer_threshold
                        },
                        'answers.group_id': group_id
                    }
                }, {
                    '$sample': {'size': 1}  # 随机一条
                }
            ])

            speak_context = list(speak_context)
            if not speak_context:
                continue

            messages = [answer['messages']
                        for answer in speak_context[0]['answers']
                        if answer['count'] >= Chat.answer_threshold
                        and answer['group_id'] == group_id]

            if not messages:
                continue

            speak = random.choice(random.choice(messages))

            with Chat._reply_lock:
                Chat._reply_dict[group_id].append({
                    'time': int(cur_time),
                    'pre_raw_message': '[PallasBot: Speak]',
                    'pre_keywords': '[PallasBot: Speak]',
                    'reply': speak,
                    'reply_keywords': '[PallasBot: Speak]'
                })

            speak_list = [Message(speak), ]
            while random.random() < Chat.speak_continuously_probability \
                    and len(speak_list) < Chat.speak_continuously_max_len:
                pre_msg = str(speak_list[-1])
                answer = Chat(ChatData(group_id, 0, pre_msg,
                                       pre_msg, cur_time)).answer(False)
                if not answer:
                    break
                speak_list.extend(answer)
            return (group_id, speak_list)

        return None

    @staticmethod
    def ban(group_id: int, ban_raw_message: str) -> bool:
        '''
        禁止以后回复这句话，仅对该群有效果
        '''

        if group_id not in Chat._reply_dict:
            return False

        ban_reply = None
        for reply in Chat._reply_dict[group_id][::-1]:
            cur_reply = reply['reply']
            # 为空时就直接 ban 最后一条回复
            if not ban_raw_message or ban_raw_message == cur_reply:
                ban_reply = reply
                break

        # 这种情况一般是有些 CQ 码，牛牛发送的时候，和被回复的时候，里面的内容不一样
        if not ban_reply:
            search = re.search(r'(\[CQ:[a-zA-z0-9-_.]+)',
                               ban_raw_message)
            if search:
                type_keyword = search.group(1)
                for reply in Chat._reply_dict[group_id][::-1]:
                    cur_reply = reply['reply']
                    if type_keyword in cur_reply:
                        ban_reply = reply
                        break

        if not ban_reply:
            return False

        pre_keywords = reply['pre_keywords']
        keywords = reply['reply_keywords']

        # 考虑这句回复是从别的群捞过来的情况，所以这里要分两次 update
        # context_mongo.update_one({
        #     'keywords': pre_keywords,
        #     'answers.keywords': keywords,
        #     'answers.group_id': group_id
        # }, {
        #     '$set': {
        #         'answers.$.count': -99999
        #     }
        # })
        context_mongo.update_one({
            'keywords': pre_keywords
        }, {
            '$push': {
                'ban': {
                    'keywords': keywords,
                    'group_id': group_id
                }
            }
        })
        if keywords in Chat.blacklist_answer_reserve[group_id]:
            Chat.blacklist_answer[group_id].add(keywords)
            if keywords in Chat.blacklist_answer_reserve[Chat._blacklist_flag]:
                Chat.blacklist_answer[Chat._blacklist_flag].add(
                    keywords)
        else:
            Chat.blacklist_answer_reserve[group_id].add(keywords)

        return True

    @staticmethod
    def drink(group_id: int) -> None:
        '''
        牛牛喝酒，仅对该群有效果。提高醉酒程度（降低回复阈值的概率）
        '''
        Chat._drunkenness_dict[group_id] += 1

    @staticmethod
    def sober_up(group_id: int) -> bool:
        '''
        牛牛醒酒，仅对该群有效果。返回醒酒是否成功
        '''

        Chat._drunkenness_dict[group_id] -= 1
        return Chat._drunkenness_dict[group_id] <= 0

# private:
    _reply_dict = defaultdict(list)  # 牛牛回复的消息缓存，暂未做持久化
    _message_dict = {}              # 群消息缓存
    _drunkenness_dict = defaultdict(int)          # 醉酒程度，不同群应用不同的数值

    _save_reserve_size = 100        # 保存时，给内存中保留的大小
    _late_save_time = 0             # 上次保存（消息数据持久化）的时刻 ( time.time(), 秒 )

    _reply_lock = threading.Lock()
    _message_lock = threading.Lock()
    _blacklist_flag = 114514

    def _message_insert(self):
        group_id = self.chat_data.group_id

        with Chat._message_lock:
            if group_id not in Chat._message_dict:
                Chat._message_dict[group_id] = []

            Chat._message_dict[group_id].append({
                'group_id': group_id,
                'user_id': self.chat_data.user_id,
                'raw_message': self.chat_data.raw_message,
                'is_plain_text': self.chat_data.is_plain_text,
                'plain_text': self.chat_data.plain_text,
                'keywords': self.chat_data.keywords,
                'time': self.chat_data.time,
            })

        cur_time = self.chat_data.time
        if Chat._late_save_time == 0:
            Chat._late_save_time = cur_time - 1
            return

        if len(Chat._message_dict[group_id]) > Chat.save_count_threshold:
            Chat._sync(cur_time)

        elif cur_time - Chat._late_save_time > Chat.save_time_threshold:
            Chat._sync(cur_time)

    @staticmethod
    def _sync(cur_time: int = time.time()):
        '''
        持久化
        '''

        with Chat._message_lock:
            save_list = [msg
                         for group_msgs in Chat._message_dict.values()
                         for msg in group_msgs
                         if msg['time'] > Chat._late_save_time]
            if not save_list:
                return

            Chat._message_dict = {group_id: group_msgs[-Chat._save_reserve_size:]
                                  for group_id, group_msgs in Chat._message_dict.items()}

            Chat._late_save_time = cur_time

        message_mongo.insert_many(save_list)

    def _context_insert(self, pre_msg):
        if not pre_msg:
            return

        raw_message = self.chat_data.raw_message

        # 在复读，不学
        if pre_msg['raw_message'] == raw_message:
            return

        # 回复别人的，不学
        if '[CQ:reply,' in raw_message:
            return

        keywords = self.chat_data.keywords
        group_id = self.chat_data.group_id
        pre_keywords = pre_msg['keywords']
        cur_time = self.chat_data.time

        # update_key = {
        #     'keywords': pre_keywords,
        #     'answers.keywords': keywords,
        #     'answers.group_id': group_id
        # }
        # update_value = {
        #     '$set': {'time': cur_time},
        #     '$inc': {'answers.$.count': 1},
        #     '$push': {'answers.$.messages': raw_message}
        # }
        # # update_value.update(update_key)

        # context_mongo.update_one(
        #     update_key, update_value, upsert=True)

        # 这个 upsert 太难写了，搞不定_(:з」∠)_
        # 先用 find + insert or update 凑合了
        find_key = {'keywords': pre_keywords}
        context = context_mongo.find_one(find_key)
        if context:
            update_value = {
                '$set': {
                    'time': cur_time
                },
                '$inc': {'count': 1}
            }
            answer_index = next((idx for idx, answer in enumerate(context['answers'])
                                 if answer['group_id'] == group_id
                                 and answer['keywords'] == keywords), -1)
            if answer_index != -1:
                update_value['$inc'].update({
                    f'answers.{answer_index}.count': 1
                })
                update_value['$set'].update({
                    f'answers.{answer_index}.time': cur_time
                })
                # 不是纯文本的时候，raw_message 是完全一样的，没必要 push
                if self.chat_data.is_plain_text:
                    update_value['$push'] = {
                        f'answers.{answer_index}.messages': raw_message
                    }
            else:
                update_value['$push'] = {
                    'answers': {
                        'keywords': keywords,
                        'group_id': group_id,
                        'count': 1,
                        'time': cur_time,
                        'messages': [
                            raw_message
                        ]
                    }
                }

            context_mongo.update_one(find_key, update_value)
        else:
            context = {
                'keywords': pre_keywords,
                'time': cur_time,
                'count': 1,
                'answers': [
                    {
                        'keywords': keywords,
                        'group_id': group_id,
                        'count': 1,
                        'time': cur_time,
                        'messages': [
                            raw_message
                        ]
                    }
                ]
            }
            context_mongo.insert_one(context)

    def _context_find(self) -> Optional[Tuple[List[str], str]]:

        group_id = self.chat_data.group_id
        raw_message = self.chat_data.raw_message
        keywords = self.chat_data.keywords

        # 复读！
        if group_id in Chat._message_dict:
            group_msgs = Chat._message_dict[group_id]
            if group_msgs and len(group_msgs) >= Chat.repeat_threshold:
                if all(item['raw_message'] == raw_message
                        for item in group_msgs[:-Chat.repeat_threshold:-1]):
                    # 复读过一次就不复读了
                    if group_id not in Chat._reply_dict \
                            or Chat._reply_dict[group_id][-1]['reply'] != raw_message:
                        return ([raw_message, ], keywords)

        context = context_mongo.find_one({'keywords': keywords})

        if not context:
            return None

        if Chat._drunkenness_dict[group_id] > 0:
            drunkenness = 1
        else:
            drunkenness = Chat.drunk_probability

        if random.random() < drunkenness:
            rand_threshold = 1
        else:
            rand_threshold = Chat.answer_threshold

        # 全局的黑名单
        ban_keywords = Chat.blacklist_answer[Chat._blacklist_flag] | Chat.blacklist_answer[group_id]
        # 针对单条回复的黑名单
        if 'ban' in context:
            ban_count = defaultdict(int)
            for ban in context['ban']:
                ban_key = ban['keywords']
                if ban['group_id'] == group_id or ban['group_id'] == Chat._blacklist_flag:
                    ban_keywords.add(ban_key)
                else:
                    # 超过 N 个群都把这句话 ban 了，那就全局 ban 掉
                    ban_count[ban_key] += 1
                    if ban_count[ban_key] == Chat.cross_group_threshold:
                        ban_keywords.add(ban_key)

        if not self.chat_data.is_image:
            all_answers = [answer
                           for answer in context['answers']
                           if answer['count'] >= rand_threshold
                           and len(answer['keywords']) > 1]  # 屏蔽特别短的回复内容，大部分是“？”、“草”之类的

        else:
            all_answers = [answer
                           for answer in context['answers']
                           if answer['count'] >= rand_threshold
                           and answer['keywords'].startswith('[CQ:')]   # 屏蔽图片后的纯文字回复，图片经常是表情包，后面的纯文字什么都有，很乱

        candidate_answers = {}
        other_group_cache = {}
        answers_count = defaultdict(int)

        def candidate_append(dst, answer):
            answer_key = answer['keywords']
            if answer_key not in dst:
                dst[answer_key] = answer
            else:
                pre_answer = dst[answer_key]
                pre_answer['count'] += answer['count']
                pre_answer['messages'] += answer['messages']

        cross_group_threshold = Chat.cross_group_threshold
        # if self.chat_data.to_me:
        #     cross_group_threshold = 1

        for answer in all_answers:
            answer_key = answer['keywords']
            if answer_key in ban_keywords:
                continue

            if answer['group_id'] == group_id:
                candidate_append(candidate_answers, answer)
            # 别的群的 at, 忽略
            elif '[CQ:at,qq=' in answer_key:
                continue
            else:   # 有这么 N 个群都有相同的回复，就作为全局回复
                answers_count[answer_key] += 1
                cur_count = answers_count[answer_key]
                if cur_count < cross_group_threshold:      # 没达到阈值前，先缓存
                    candidate_append(other_group_cache, answer)
                elif cur_count == cross_group_threshold:   # 刚达到阈值时，将缓存加入
                    if cur_count > 1:
                        candidate_append(candidate_answers,
                                         other_group_cache[answer_key])
                    candidate_append(candidate_answers, answer)
                else:                                      # 超过阈值后，加入
                    candidate_append(candidate_answers, answer)

        if not candidate_answers:
            return None

        final_answer = random.choices(list(candidate_answers.values()), weights=[
            # 防止某个回复权重太大，别的都 Roll 不到了
            min(answer['count'], 10) for answer in candidate_answers.values()])[0]
        answer_str = random.choice(final_answer['messages'])
        answer_keywords = final_answer['keywords']

        if 0 < answer_str.count('，') <= 3 and random.random() < Chat.split_probability:
            return (answer_str.split('，'), answer_keywords)
        return ([answer_str, ], answer_keywords)

    @staticmethod
    def _text_to_speech(text: str) -> Optional[Message]:
        # if plugin_config.enable_voice:
        #     result = tts_client.synthesis(text, options={'per': 111})  # 度小萌
        #     if not isinstance(result, dict):  # error message
        #         return MessageSegment.record(result)

        return Message(f'[CQ:tts,text={text}]')

    @staticmethod
    def update_global_blacklist() -> None:
        Chat._select_blacklist()

        keywords_dict = defaultdict(int)
        global_blacklist = set()
        for _, keywords_list in Chat.blacklist_answer.items():
            for keywords in keywords_list:
                keywords_dict[keywords] += 1
                if keywords_dict[keywords] == Chat.cross_group_threshold:
                    global_blacklist.add(keywords)

        Chat.blacklist_answer[Chat._blacklist_flag] |= global_blacklist

    @staticmethod
    def _select_blacklist() -> None:
        all_blacklist = blacklist_mongo.find()

        for item in all_blacklist:
            group_id = item['group_id']
            if 'answers' in item:
                Chat.blacklist_answer[group_id] |= set(item['answers'])
            if 'answers_reserve' in item:
                Chat.blacklist_answer_reserve[group_id] |= set(
                    item['answers_reserve'])

    @staticmethod
    def _sync_blacklist() -> None:
        Chat._select_blacklist()

        for group_id, answers in Chat.blacklist_answer.items():
            if not len(answers):
                continue
            blacklist_mongo.update_one(
                {'group_id': group_id},
                {'$set': {'answers': list(answers)}},
                upsert=True)

        for group_id, answers in Chat.blacklist_answer_reserve.items():
            if not len(answers):
                continue
            if group_id in Chat.blacklist_answer:
                answers = answers - Chat.blacklist_answer[group_id]

            blacklist_mongo.update_one(
                {'group_id': group_id},
                {'$set': {'answers_reserve': list(answers)}},
                upsert=True)

    @staticmethod
    def clearup_context() -> None:
        '''
        清理所有超过 30 天没人说、且没有学会的话
        '''

        cur_time = int(time.time())
        expiration = cur_time - 30 * 24 * 3600  # 三十天前

        context_mongo.delete_many({
            'time': {'$lt': expiration},
            'count': {'$lt': Chat.answer_threshold}    # lt 是小于，不包括等于
        })

        all_context = context_mongo.find({
            'count': {'$gt': 100},
            '$or': [
                # 历史遗留问题，老版本的数据没有 clear_time 字段
                {"clear_time": {"$exists": False}},
                {"clear_time": {"$lt": expiration}}
            ]
        })
        for context in all_context:
            answers = [ans
                       for ans in context['answers']
                       # 历史遗留问题，老版本的数据没有 answers.$.time 字段
                       if ans['count'] > 1 or ('time' in ans and ans['time'] > expiration)]
            context_mongo.update_one({
                'keywords': context['keywords']
            }, {
                '$set': {
                    'answers': answers,
                    'clear_time': cur_time
                }
            })

    @staticmethod
    def completely_sober():
        for key in Chat._drunkenness_dict.keys():
            Chat._drunkenness_dict[key] = 0


# Auto sync on program start
Chat.update_global_blacklist()


def _chat_sync():
    Chat._sync()
    Chat._sync_blacklist()


# Auto sync on program exit
atexit.register(_chat_sync)


if __name__ == '__main__':

    Chat.clearup_context()
    # # while True:
    # test_data: ChatData = ChatData(
    #     group_id=1234567,
    #     user_id=1111111,
    #     raw_message='牛牛出来玩',
    #     plain_text='牛牛出来玩',
    #     time=time.time()
    # )

    # test_chat: Chat = Chat(test_data)

    # print(test_chat.answer())
    # test_chat.learn()

    # test_answer_data: ChatData = ChatData(
    #     group_id=1234567,
    #     user_id=1111111,
    #     raw_message='别烦',
    #     plain_text='别烦',
    #     time=time.time()
    # )

    # test_answer: Chat = Chat(test_answer_data)
    # print(test_chat.answer())
    # test_answer.learn()

    # time.sleep(5)
    # print(Chat.speak())
