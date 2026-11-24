return {
  {
    dir = vim.fn.stdpath("config") .. "/lua/helm",
    name = "helm",
    lazy = false,
    config = function()
      require("helm").setup({ width = 52 })
      local ok, wk = pcall(require, "which-key")
      if ok then wk.add({ { "<leader>k", group = "helm", icon = "󰭻" } }) end
    end,
    keys = {
      { "<leader>kk", "<cmd>Helm<cr>",        desc = "Open helm" },
      { "<leader>kn", "<cmd>Helm new<cr>",    desc = "New conversation" },
      { "<leader>kt", "<cmd>Helm topic<cr>",  desc = "New topic" },
      { "<leader>ki", "<cmd>Helm import<cr>", desc = "Bring back a past conversation" },
      { "<leader>k?", "<cmd>Helm keys<cr>",   desc = "Helm keys" },
    },
  },
}
