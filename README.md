obsutil config \
  -e=obs.cn-east-3.myhuaweicloud.com \
  -i=HPUAK60UTFPAERUJENTV \
  -k=aX7FW1J23JSJ1EvYEVSXBZnGxu5uQ0F9wcrZLMEP \
  -config=/home/app-dev/.obsutil_shanghai/.obsutilconfig_shanghai



obsutil config \
  -e=obs.cn-east-3.myhuaweicloud.com \
  -i=HPUARD8TNC6O4RAYM1SX \
  -k=qQXkp9YaZLTIwlHxbk4mbLezL0z7J1ZHgPwZJu54 \
  -config=/home/app-dev/.obsutil_zhengzhou/.obsutilconfig_zhengzhou


mkdir /home/app-dev/.obsutil_shanghai/
mkdir /home/app-dev/.obsutil_zhengzhou/

code /home/app-dev/.obsutil_shanghai/.obsutilconfig_shanghai
code /home/app-dev/.obsutil_zhengzhou/.obsutilconfig_zhengzhou

修改现在的按sn分类和按sn排序，现在一个sn的界定标准是sn以及area都不同，所有的sn的格式变为 area: sn

在上方加一个选项，多图显示和单图显示模式，默认是单图
多图模式下只能选中rulecode这一层，不会展开具体的字段，画面上会显示x张这个rulecode的图（大于等于2,可以设定），并且有一个翻页功能可以翻页继续显示后面没有显示的这个rulecode的其他字段的图
和我敲定实现细节




































COBOTMAGICV2.0 缺少关节静止区间 需要后续修改collect的代码，不检测robot（顺便也让关节静止区间返回一个静止比例，之后可以检查静止比例是否超过百分之30）
DWHEEL-TACTILE fps设定30fps 通过率低
PIKA 深度相机的fps波动过大，历史数据中的fps在0-60之间波动
QINGLOONGV2.5 百分之70的数据缺少effector的velocity字段，检查是否是正常情况
  - 
S1 001和002的话题和group命名不统一，后续需要统一为002的ros格式
UR 百分之80的数据缺少effort和velocity数据，检查是否正常
  - 1月29号之后出现velocity和effort
  - 只在1月15号-1月28号之间存在end


可视化界面需要添加下载json的功能

搜索字段功能

在写入数据库的时候按照一定规则来排序字段
