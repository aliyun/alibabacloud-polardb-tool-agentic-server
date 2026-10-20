import type { EndpointCatalog } from '../api/aliyunEndpoints'

export const endpointCatalog: EndpointCatalog = {
  reviewed_at: '2026-09-24',
  sources: { polardb: 'https://api.aliyun.com/product/polardb?tab=endpoints', sts: 'https://help.aliyun.com/zh/ram/developer-reference/api-sts-2015-04-01-endpoint' },
  regions: [
    { region_id: 'cn-hangzhou', label: { 'zh-CN': '华东1（杭州）', en: 'China (Hangzhou)' },
      polardb: { public: 'polardb.aliyuncs.com', vpc: 'polardb-vpc.cn-hangzhou.aliyuncs.com' },
      sts: { public: 'sts.cn-hangzhou.aliyuncs.com', vpc: 'sts-vpc.cn-hangzhou.aliyuncs.com' } },
    { region_id: 'cn-beijing', label: { 'zh-CN': '华北2（北京）', en: 'China (Beijing)' },
      polardb: { public: 'polardb.aliyuncs.com', vpc: 'polardb-vpc.cn-beijing.aliyuncs.com' },
      sts: { public: 'sts.cn-beijing.aliyuncs.com', vpc: 'sts-vpc.cn-beijing.aliyuncs.com' } },
    { region_id: 'cn-shenzhen', label: { 'zh-CN': '华南1（深圳）', en: 'China (Shenzhen)' },
      polardb: { public: 'polardb.cn-shenzhen.aliyuncs.com', vpc: 'polardb-vpc.cn-shenzhen.aliyuncs.com' },
      sts: { public: 'sts.cn-shenzhen.aliyuncs.com', vpc: 'sts-vpc.cn-shenzhen.aliyuncs.com' } },
    { region_id: 'cn-hangzhou-finance', label: { 'zh-CN': '华东1 金融云', en: 'China (Hangzhou) Finance' },
      polardb: { public: 'polardb.aliyuncs.com', vpc: 'polardb-vpc.cn-hangzhou-finance.aliyuncs.com' },
      sts: { public: 'sts.cn-hangzhou-finance.aliyuncs.com', vpc: null } },
  ],
}
