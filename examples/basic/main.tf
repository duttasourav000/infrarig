# Hand-written companion to generated.tf: the root provider and the
# variables the generated HCL expects. Replace the placeholder values.

provider "aws" {
  region = "us-east-1" # must match the first region declared in the .rig file
}

variable "subnet_ids" {
  type    = list(string)
  default = ["subnet-0123456789abcdef0"] # placeholder
}
