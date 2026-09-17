export interface Page<T> {
  items: T[]
  total: number
  offset: number
  limit: number
}

export interface PageParams {
  offset?: number
  limit?: number
  search?: string
}
